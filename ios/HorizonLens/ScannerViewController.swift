import AVFoundation
import UIKit

final class ScannerViewController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    var onLink: ((PairLink) -> Void)?

    private let session = AVCaptureSession()
    private var preview: AVCaptureVideoPreviewLayer?
    private let hint = UILabel()
    private var done = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        hint.text = "Scan the QR code shown in HorizonLens on your PC"
        hint.textColor = .white
        hint.font = .systemFont(ofSize: 17, weight: .semibold)
        hint.numberOfLines = 0
        hint.textAlignment = .center
        hint.translatesAutoresizingMaskIntoConstraints = false

        let manual = UIButton(type: .system)
        manual.setTitle("Enter the link instead", for: .normal)
        manual.titleLabel?.font = .systemFont(ofSize: 15, weight: .medium)
        manual.tintColor = UIColor(red: 1.0, green: 0.62, blue: 0.2, alpha: 1)
        manual.addTarget(self, action: #selector(enterManually), for: .touchUpInside)
        manual.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(hint)
        view.addSubview(manual)
        NSLayoutConstraint.activate([
            hint.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 24),
            hint.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -24),
            hint.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 32),
            manual.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            manual.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -28),
        ])
        startCamera()
    }

    private func startCamera() {
        AVCaptureDevice.requestAccess(for: .video) { granted in
            DispatchQueue.main.async {
                guard granted else {
                    self.hint.text = "Allow the camera for HorizonLens in Settings to scan the code, or enter the link."
                    return
                }
                self.configure()
            }
        }
    }

    private func configure() {
        guard let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device),
              session.canAddInput(input) else { return }
        session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { return }
        session.addOutput(output)
        output.setMetadataObjectsDelegate(self, queue: .main)
        output.metadataObjectTypes = [.qr]
        let layer = AVCaptureVideoPreviewLayer(session: session)
        layer.videoGravity = .resizeAspectFill
        layer.frame = view.bounds
        view.layer.insertSublayer(layer, at: 0)
        preview = layer
        DispatchQueue.global(qos: .userInitiated).async { self.session.startRunning() }
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        preview?.frame = view.bounds
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        stop()
    }

    func stop() {
        if session.isRunning {
            DispatchQueue.global(qos: .userInitiated).async { self.session.stopRunning() }
        }
    }

    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard !done else { return }
        for case let code as AVMetadataMachineReadableCodeObject in objects {
            guard let text = code.stringValue else { continue }
            if let link = PairLink(text) {
                finish(link)
                return
            }
            hint.text = "That isn't a HorizonLens code. Scan the QR code in HorizonLens on your PC."
        }
    }

    @objc private func enterManually() {
        let alert = UIAlertController(title: "PC link",
                                      message: "Type the link shown under the QR code in HorizonLens, for example 192.168.1.20:8787",
                                      preferredStyle: .alert)
        alert.addTextField { field in
            field.keyboardType = .URL
            field.autocapitalizationType = .none
            field.autocorrectionType = .no
            field.placeholder = "192.168.1.20:8787"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(UIAlertAction(title: "Connect", style: .default) { _ in
            var text = alert.textFields?.first?.text?.trimmingCharacters(in: .whitespaces) ?? ""
            if !text.lowercased().hasPrefix("https://") { text = "https://" + text }
            if let link = PairLink(text) {
                self.finish(link)
            } else {
                self.hint.text = "That link isn't a PC on your Wi-Fi. Check the address in HorizonLens."
            }
        })
        present(alert, animated: true)
    }

    private func finish(_ link: PairLink) {
        done = true
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        stop()
        onLink?(link)
    }
}
