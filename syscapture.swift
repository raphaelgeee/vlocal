// syscapture — helper HEADLESS de capture de l'audio systeme (ScreenCaptureKit).
// Embarque dans une app (Vlocal) et lance en SOUS-PROCESS : l'app a le grant
// « Enregistrement de l'ecran » ; le helper, lance par elle, en herite.
//
// DEUX MODES :
//   syscapture -            [duree]   -> FLUX : PCM mono float32 sur STDOUT (chemin LIVE)
//   syscapture <fichier.wav> [duree]  -> FICHIER : ecrit un WAV (Phase 0 / repli)
//
// Mode FLUX (le chemin live de Vlocal) :
//   - demande SCStreamConfiguration.sampleRate=16000, channelCount=1 (mono, basse RAM,
//     debit /6) ; mais c'est une PREFERENCE -> on emet un HANDSHAKE sur stderr
//     « FMT <sr_reel> 1 f32 » AVANT le 1er echantillon, et on le re-emet si le format
//     change a chaud. Le lecteur Python ne re-echantillonne QUE si sr_reel != 16000.
//   - downmix systematique en MONO -> Python n'a jamais a desentrelacer.
//   - PCM float32 brut ecrit sur stdout (serialise) ; SIGTERM -> ferme stdout (EOF).
//   stderr : « FMT … », « READY », « START_FAIL … », « ERR … », « FRAMES <n> ».
//   exit : 0 si frames>0, 2 si 0 frame (permission/silence total), 1 echec demarrage.

import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia

let WANT_SR: Double = 16000
let WANT_CH = 1

final class SystemAudioCapture: NSObject, SCStreamDelegate, SCStreamOutput {
    private var stream: SCStream?
    private let q = DispatchQueue(label: "syscap.audio")   // serialise sortie + close
    private(set) var frames: Int = 0

    // sortie : soit stdout (flux), soit un AVAudioFile (fichier)
    private let toStdout: Bool
    private let fileURL: URL?
    private var file: AVAudioFile?
    private let out = FileHandle.standardOutput
    private let err = FileHandle.standardError
    private var lastSR: Double = 0
    private var lastCh: Int = 0

    init(stdout: Bool, fileURL: URL?) { self.toStdout = stdout; self.fileURL = fileURL }

    private func logErr(_ s: String) { err.write((s + "\n").data(using: .utf8)!) }

    func start() async throws {
        let content = try await SCShareableContent.current
        guard let display = content.displays.first else {
            throw NSError(domain: "syscapture", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "aucun ecran"])
        }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.excludesCurrentProcessAudio = true
        cfg.sampleRate = Int(WANT_SR)     // PREFERENCE (verifiee via handshake FMT)
        cfg.channelCount = WANT_CH
        cfg.width = 2; cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 6)
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: q)
        try await s.startCapture()
        self.stream = s
    }

    func stop() async {
        if let s = stream { try? await s.stopCapture() }
        q.sync {
            file = nil
            if toStdout { try? out.close() }   // EOF cote lecteur
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer),
              let fmtDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc),
              let format = AVAudioFormat(streamDescription: asbd) else { return }
        let n = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard n > 0, let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: n) else { return }
        pcm.frameLength = n
        if CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(n), into: pcm.mutableAudioBufferList) != noErr { return }
        // deja sur la file q -> serialise

        let sr = format.sampleRate
        let ch = Int(format.channelCount)
        if toStdout {
            // Handshake FMT (avant le 1er chunk, re-emis si le format change a chaud).
            if sr != lastSR || ch != lastCh {
                logErr("FMT \(Int(sr)) 1 f32")     // on emet TOUJOURS du mono
                lastSR = sr; lastCh = ch
            }
            // Downmix -> mono float32, ecriture brute sur stdout.
            let frameN = Int(n)
            var mono = [Float](repeating: 0, count: frameN)
            if let chans = pcm.floatChannelData {
                if ch <= 1 {
                    mono.withUnsafeMutableBufferPointer { m in
                        m.baseAddress!.update(from: chans[0], count: frameN)
                    }
                } else {
                    for c in 0..<ch {
                        let p = chans[c]
                        for i in 0..<frameN { mono[i] += p[i] }
                    }
                    let inv = 1.0 / Float(ch)
                    for i in 0..<frameN { mono[i] *= inv }
                }
                mono.withUnsafeBytes { raw in out.write(Data(raw)) }
                frames += frameN
            }
        } else {
            do {
                if file == nil, let u = fileURL {
                    file = try AVAudioFile(forWriting: u, settings: format.settings)
                }
                try file?.write(from: pcm)
                frames += Int(n)
            } catch { }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logErr("ERR \(error.localizedDescription)")
    }
}

// ---- main ----
let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("usage: syscapture <out.wav|-> [seconds]\n".data(using: .utf8)!)
    exit(64)
}
let stdoutMode = (args[1] == "-" || args[1] == "--stdout")
let fileURL: URL? = stdoutMode ? nil : URL(fileURLWithPath: args[1])
let duration: Double? = args.count >= 3 ? Double(args[2]) : nil

let cap = SystemAudioCapture(stdout: stdoutMode, fileURL: fileURL)

func finishAndExit() {
    let sem = DispatchSemaphore(value: 0)
    Task { await cap.stop(); sem.signal() }
    sem.wait()
    FileHandle.standardError.write("FRAMES \(cap.frames)\n".data(using: .utf8)!)
    exit(cap.frames > 0 ? 0 : 2)
}

signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
let sigInt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigTerm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigInt.setEventHandler { finishAndExit() }
sigTerm.setEventHandler { finishAndExit() }
sigInt.resume(); sigTerm.resume()

let startSem = DispatchSemaphore(value: 0)
Task {
    do {
        try await cap.start()
        FileHandle.standardError.write("READY\n".data(using: .utf8)!)
    } catch {
        FileHandle.standardError.write("START_FAIL \(error.localizedDescription)\n".data(using: .utf8)!)
        exit(1)
    }
    startSem.signal()
}
startSem.wait()

if let d = duration {
    DispatchQueue.main.asyncAfter(deadline: .now() + d) { finishAndExit() }
}
RunLoop.main.run()
