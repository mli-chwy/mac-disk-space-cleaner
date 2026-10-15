import Foundation
import Darwin

if CommandLine.arguments.count < 2 { exit(2) }
let fm = FileManager.default
for item in CommandLine.arguments.dropFirst() {
    let url = URL(fileURLWithPath: item)
    do {
        var result: NSURL?
        try fm.trashItem(at: url, resultingItemURL: &result)
    } catch {
        fputs("trash failed: \(item)\n", stderr)
        exit(1)
    }
}
