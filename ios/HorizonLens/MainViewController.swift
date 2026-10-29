import ARKit
import UIKit
import WebKit

final class MainViewController: UIViewController, WKNavigationDelegate, WKUIDelegate,
                                WKScriptMessageHandler, ARSessionDelegate {
    private var webView: WKWebView!
    private let arSession = ARSession()
    private var arRunning = false
    private var jsBusy = false
    private var link: PairLink?
    private var pageReady = false
    private let errorView = ErrorView()

    override var prefersStatusBarHidden: Bool { true }
    override var prefersHomeIndicatorAutoHidden: Bool { true }
    override var preferredScreenEdgesDeferringSystemGestures: UIRectEdge { .bottom }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        makeWebView()

        errorView.isHidden = true
        errorView.frame = view.bounds
        errorView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        errorView.onRetry = { [weak self] in self?.reload() }
        errorView.onScan = { [weak self] in self?.showScanner() }
        view.addSubview(errorView)

        arSession.delegate = self
        arSession.delegateQueue = .main

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(appActive),
                       name: UIApplication.didBecomeActiveNotification, object: nil)
        nc.addObserver(self, selector: #selector(appInactive),
                       name: UIApplication.willResignActiveNotification, object: nil)
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        if link == nil {
            if let saved = PairLink.load() { open(saved) } else { showScanner() }
        }
    }

    private func makeWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.userContentController.add(WeakScriptHandler(self), name: "horizonlens")
        let web = WKWebView(frame: view.bounds, configuration: config)
        web.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        web.isOpaque = false
        web.backgroundColor = .black
        web.scrollView.backgroundColor = .black
        web.scrollView.isScrollEnabled = false
        web.scrollView.bounces = false
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.navigationDelegate = self
        web.uiDelegate = self
        view.addSubview(web)
        webView = web
    }

    func open(_ newLink: PairLink) {
        if let presented = presentedViewController { presented.dismiss(animated: true) }
        link = newLink
        newLink.save()
        reload()
    }

    private func reload() {
        guard let link else { showScanner(); return }
        errorView.isHidden = true
        pageReady = false
        webView.load(URLRequest(url: link.url, cachePolicy: .reloadIgnoringLocalCacheData,
                                timeoutInterval: 10))
    }

    private func showScanner() {
        stopAR()
        guard presentedViewController == nil else { return }
        let scanner = ScannerViewController()
        scanner.modalPresentationStyle = .fullScreen
        scanner.onLink = { [weak self] link in self?.open(link) }
        present(scanner, animated: true)
    }

    private func showError(_ message: String) {
        stopAR()
        errorView.message.text = message
        errorView.isHidden = false
    }

    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = space.serverTrust, var current = link, space.host == current.host else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        if current.accepts(trust) {
            if current != link { link = current; current.save() }
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            showError("This isn't the PC you paired with (its security certificate changed). Scan the QR code again.")
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageReady = true
        errorView.isHidden = true
        startAR()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        guard errorView.isHidden else { return }
        showError("Can't reach HorizonLens on your PC.\nCheck that HorizonLens is running (press Start) and this phone is on the same Wi-Fi.")
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showError("Lost the connection to your PC.")
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { reload() }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "OK", style: .destructive) { _ in completionHandler(true) })
        present(alert, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(alert, animated: true)
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        switch cmd {
        case "scan", "forget":
            PairLink.forget()
            link = nil
            showScanner()
        default:
            break
        }
    }

    private func startAR() {
        guard pageReady, !arRunning, ARWorldTrackingConfiguration.isSupported else { return }
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity

        if let small = ARWorldTrackingConfiguration.supportedVideoFormats.min(by: {
            $0.imageResolution.width * $0.imageResolution.height
                < $1.imageResolution.width * $1.imageResolution.height
        }) {
            config.videoFormat = small
        }
        arSession.run(config, options: [.resetTracking, .removeExistingAnchors])
        arRunning = true
    }

    private func stopAR() {
        guard arRunning else { return }
        arSession.pause()
        arRunning = false
        jsBusy = false
        webView?.evaluateJavaScript("window.HLTracking && HLTracking.nativeStopped()")
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard arRunning, !jsBusy else { return }
        let orientation = view.window?.windowScene?.interfaceOrientation ?? .portrait

        let m = frame.camera.viewMatrix(for: orientation).inverse
        let rotation = simd_quatf(simd_float3x3(
            SIMD3(m.columns.0.x, m.columns.0.y, m.columns.0.z),
            SIMD3(m.columns.1.x, m.columns.1.y, m.columns.1.z),
            SIMD3(m.columns.2.x, m.columns.2.y, m.columns.2.z)))
        var ok = false
        if case .normal = frame.camera.trackingState { ok = true }
        let p = m.columns.3
        let js = String(format: "window.HLTracking&&HLTracking.nativePose(%.6f,%.6f,%.6f,%.6f,%.4f,%.4f,%.4f,%d)",
                        rotation.imag.x, rotation.imag.y, rotation.imag.z, rotation.real,
                        p.x, p.y, p.z, ok ? 1 : 0)
        jsBusy = true
        webView.evaluateJavaScript(js) { [weak self] _, _ in self?.jsBusy = false }
    }

    func sessionWasInterrupted(_ session: ARSession) {

        webView.evaluateJavaScript("window.HLTracking && HLTracking.nativeStopped()")
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        arRunning = false
        webView.evaluateJavaScript("window.HLTracking && HLTracking.nativeStopped()")
    }

    @objc private func appActive() { if pageReady { startAR() } }
    @objc private func appInactive() { stopAR() }
}

private final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(controller, didReceive: message)
    }
}

private final class ErrorView: UIView {
    let message = UILabel()
    var onRetry: (() -> Void)?
    var onScan: (() -> Void)?

    override init(frame: CGRect) {
        super.init(frame: frame)
        backgroundColor = .black
        message.textColor = .white
        message.font = .systemFont(ofSize: 17, weight: .medium)
        message.numberOfLines = 0
        message.textAlignment = .center

        let orange = UIColor(red: 1.0, green: 0.62, blue: 0.2, alpha: 1)
        let retry = UIButton(type: .system)
        retry.setTitle("Try again", for: .normal)
        retry.titleLabel?.font = .systemFont(ofSize: 17, weight: .semibold)
        retry.backgroundColor = orange
        retry.setTitleColor(.black, for: .normal)
        retry.layer.cornerRadius = 22
        retry.addTarget(self, action: #selector(retryTapped), for: .touchUpInside)

        let scan = UIButton(type: .system)
        scan.setTitle("Scan the QR code again", for: .normal)
        scan.tintColor = orange
        scan.addTarget(self, action: #selector(scanTapped), for: .touchUpInside)

        let stack = UIStackView(arrangedSubviews: [message, retry, scan])
        stack.axis = .vertical
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 32),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -32),
            retry.heightAnchor.constraint(equalToConstant: 44),
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    @objc private func retryTapped() { onRetry?() }
    @objc private func scanTapped() { onScan?() }
}
