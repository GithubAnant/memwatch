// Renders the app icon (a white memory chip with a bite out of it, on a black squircle) to argv[1],
// and the menu bar template version (black chip, transparent) to argv[2].
import AppKit

func rr(_ x: Double, _ y: Double, _ w: Double, _ h: Double, _ r: Double) -> NSBezierPath {
    NSBezierPath(roundedRect: NSRect(x: x, y: y, width: w, height: h), xRadius: r, yRadius: r)
}
func circle(_ x: Double, _ y: Double, _ r: Double) -> NSBezierPath { NSBezierPath(ovalIn: NSRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r)) }

// Chip in 1024-unit space, spanning 262...762.
func chip(_ color: NSColor) {
    let ctx = NSGraphicsContext.current!.cgContext
    ctx.beginTransparencyLayer(auxiliaryInfo: nil)
    color.setFill()
    rr(322, 322, 380, 380, 64).fill()
    for i in 0..<4 {
        let o = 360 + Double(i) * 88
        rr(o, 262, 40, 80, 14).fill(); rr(o, 682, 40, 80, 14).fill()
        rr(262, o, 80, 40, 14).fill(); rr(682, o, 80, 40, 14).fill()
    }
    ctx.setBlendMode(.destinationOut); NSColor.black.setFill()
    rr(442, 442, 140, 140, 24).fill()
    circle(730, 730, 170).fill()
    for deg in stride(from: 180.0, through: 270.0, by: 30.0) {
        let a = deg * .pi / 180
        circle(730 + 170 * cos(a), 730 + 170 * sin(a), 44).fill()
    }
    ctx.setBlendMode(.normal)
    ctx.endTransparencyLayer()
}

func render(_ px: Int, to path: String, _ draw: () -> Void) {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    draw()
    NSGraphicsContext.current = nil
    try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
}

render(1024, to: CommandLine.arguments[1]) {
    let body = NSRect(x: 100, y: 100, width: 824, height: 824)
    NSGraphicsContext.saveGraphicsState()
    let sh = NSShadow(); sh.shadowColor = NSColor.black.withAlphaComponent(0.28); sh.shadowBlurRadius = 22; sh.shadowOffset = NSSize(width: 0, height: -10); sh.set()
    NSColor(white: 0.04, alpha: 1).setFill(); NSBezierPath(roundedRect: body, xRadius: 186, yRadius: 186).fill()
    NSGraphicsContext.restoreGraphicsState()
    let rim = NSBezierPath(roundedRect: body.insetBy(dx: 1.5, dy: 1.5), xRadius: 185, yRadius: 185); rim.lineWidth = 3
    NSColor(white: 1, alpha: 0.14).setStroke(); rim.stroke()
    chip(.white)
}

// 36px = 18pt @2x. Scale the 500-unit chip to 32px, centered.
render(36, to: CommandLine.arguments[2]) {
    let t = NSAffineTransform(); t.translateX(by: 2, yBy: 2); t.scale(by: 32 / 500); t.translateX(by: -262, yBy: -262); t.concat()
    chip(.black)
}
