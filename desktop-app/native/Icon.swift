import AppKit
import Foundation
let side=1024
let image=NSImage(size:NSSize(width:side,height:side))
image.lockFocus()
NSColor(calibratedRed:0.94,green:0.96,blue:0.93,alpha:1).setFill()
NSBezierPath(roundedRect:NSRect(x:62,y:62,width:900,height:900),xRadius:204,yRadius:204).fill()
NSColor(calibratedRed:0.15,green:0.33,blue:0.26,alpha:1).setFill()
for pts:[(CGFloat,CGFloat)] in [[(222,692),(405,766),(405,355),(222,277)],[(445,764),(579,697),(579,278),(445,345)],[(618,697),(800,766),(800,351),(618,280)]] {
    let p=NSBezierPath();p.move(to:NSPoint(x:pts[0].0,y:pts[0].1))
    for (x,y) in pts.dropFirst(){p.line(to:NSPoint(x:x,y:y))};p.close();p.fill()
}
image.unlockFocus()
let rep=NSBitmapImageRep(data:image.tiffRepresentation!)!
try rep.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
