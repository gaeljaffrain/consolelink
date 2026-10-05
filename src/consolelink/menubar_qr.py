"""A QR code of the server address for the macOS menu-bar app: another device (phone, tablet)
scans it to open the page. macOS only, and no extra dependency: the code comes from Core Image's
own QR generator, loaded straight from the system framework (the PyObjC packages installed with
rumps don't include a CoreImage binding)."""
import math

import objc
from AppKit import (NSApplication, NSBackingStoreBuffered, NSBitmapImageRep, NSCIImageRep, NSColor,
                    NSFloatingWindowLevel, NSGraphicsContext, NSImage, NSImageInterpolationNone,
                    NSImageScaleProportionallyUpOrDown, NSImageView, NSPanel, NSRectFill,
                    NSTextField, NSTextAlignmentCenter, NSView, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskTitled)
from Foundation import NSData

QUIET_ZONE = 4  # modules of white around the code, as the QR standard asks
RETINA_PIXELS = 560  # draw at least this many pixels: crisp at 280 pt on a 2x screen
WINDOW_POINTS = 280

_CIFilter = None


class QRUnavailable(Exception):
    """Core Image's QR generator can't be loaded or used."""


def _core_image_filter():
    global _CIFilter
    if _CIFilter is None:
        try:
            objc.loadBundle("CoreImage", {},
                            bundle_path="/System/Library/Frameworks/CoreImage.framework")
            _CIFilter = objc.lookUpClass("CIFilter")
        except (objc.error, ImportError) as e:
            raise QRUnavailable(f"Core Image is not available: {e}") from e
    return _CIFilter


def warm_up():
    """Load Core Image and draw one code now. Call it before the USB poll thread starts: the first
    use costs ~0.4 s during which loading the framework holds Python's global lock, so the poll
    thread is frozen mid-exchange with the console, which then answers with a stalled pipe (seen
    as "Pipe error"). Raises QRUnavailable."""
    qr_bitmap("http://localhost")


def qr_bitmap(text, min_pixels=RETINA_PIXELS):
    """The QR code for `text` as a square NSBitmapImageRep: black modules on white, a quiet zone
    around it, every module a whole number of pixels (so the edges stay sharp), at least
    `min_pixels` wide."""
    message = text.encode("utf-8")
    qr_filter = _core_image_filter().filterWithName_("CIQRCodeGenerator")
    if qr_filter is None:
        raise QRUnavailable("Core Image has no QR code generator")
    qr_filter.setValue_forKey_(NSData.dataWithBytes_length_(message, len(message)), "inputMessage")
    qr_filter.setValue_forKey_("M", "inputCorrectionLevel")
    code = qr_filter.outputImage()
    modules = int(code.extent().size.width)
    scale = max(1, math.ceil(min_pixels / (modules + 2 * QUIET_ZONE)))
    pixels = (modules + 2 * QUIET_ZONE) * scale
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, pixels, pixels, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)
    NSGraphicsContext.saveGraphicsState()
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.setCurrentContext_(context)
    context.setImageInterpolation_(NSImageInterpolationNone)
    NSColor.whiteColor().setFill()
    NSRectFill(((0, 0), (pixels, pixels)))
    NSCIImageRep.imageRepWithCIImage_(code).drawInRect_(
        ((QUIET_ZONE * scale, QUIET_ZONE * scale), (modules * scale, modules * scale)))
    NSGraphicsContext.restoreGraphicsState()
    return rep


def qr_image(text, points=WINDOW_POINTS):
    """The QR code for `text` as an NSImage `points` wide (drawn at 2x for Retina screens)."""
    image = NSImage.alloc().initWithSize_((points, points))
    image.addRepresentation_(qr_bitmap(text, 2 * points))
    return image


class QRWindow:
    """One small floating window showing the QR code and the address under it; reused every time."""

    def __init__(self):
        self.panel = None
        self._image_view = None
        self._label = None
        self.url = None

    def _build(self):
        margin, label_h = 20, 36
        width = WINDOW_POINTS + 2 * margin
        height = WINDOW_POINTS + 2 * margin + label_h
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (width, height)), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        panel.setTitle_("ConsoleLink server address")
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setReleasedWhenClosed_(False)  # we keep it and show it again
        content = NSView.alloc().initWithFrame_(((0, 0), (width, height)))
        image_view = NSImageView.alloc().initWithFrame_(
            ((margin, margin + label_h), (WINDOW_POINTS, WINDOW_POINTS)))
        image_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        label = NSTextField.labelWithString_("")
        label.setFrame_(((margin, margin), (WINDOW_POINTS, label_h - 8)))
        label.setAlignment_(NSTextAlignmentCenter)
        label.setSelectable_(True)
        content.addSubview_(image_view)
        content.addSubview_(label)
        panel.setContentView_(content)
        panel.center()
        self.panel, self._image_view, self._label = panel, image_view, label

    def is_open(self):
        return self.panel is not None and self.panel.isVisible()

    def update(self, url):
        """Show `url`'s code (a no-op while it is already shown)."""
        if self.panel is None:
            self._build()
        if url != self.url:
            self._image_view.setImage_(qr_image(url))
            self._label.setStringValue_(url)
            self.url = url

    def show(self, url):
        """Open the window (or bring it to the front) showing `url`. The app has no Dock icon, so
        it has to be activated explicitly for the window to come to the front."""
        self.update(url)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

    def close(self):
        if self.panel is not None:
            self.panel.orderOut_(None)
