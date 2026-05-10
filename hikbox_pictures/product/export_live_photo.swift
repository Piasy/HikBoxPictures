import Foundation
import CryptoKit
import Darwin

enum ExportLivePhotoError: Error, CustomStringConvertible {
    case message(String)
    case posix(String, Int32)

    var description: String {
        switch self {
        case .message(let text):
            return text
        case .posix(let text, let code):
            return "\(text): \(String(cString: strerror(code)))"
        }
    }
}

func fileSize(_ url: URL) throws -> UInt64 {
    let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    guard let size = attrs[.size] as? NSNumber else {
        throw ExportLivePhotoError.message("无法读取文件大小：\(url.path)")
    }
    return size.uint64Value
}

func uint64LittleEndianBytes(_ value: UInt64) -> [UInt8] {
    var littleEndian = value.littleEndian
    return withUnsafeBytes(of: &littleEndian) { Array($0) }
}

func nulTerminatedUTF8(_ value: String) -> [UInt8] {
    Array(value.utf8) + [0]
}

func md5Hex(_ url: URL) throws -> String {
    var hasher = Insecure.MD5()
    let handle = try FileHandle(forReadingFrom: url)
    defer {
        try? handle.close()
    }

    while true {
        let data = try handle.read(upToCount: 1024 * 1024) ?? Data()
        if data.isEmpty {
            break
        }
        hasher.update(data: data)
    }

    return hasher.finalize().map { String(format: "%02x", $0) }.joined()
}

func writeXattr(path: String, name: String, value: [UInt8]) throws {
    let result = path.withCString { pathPointer in
        name.withCString { namePointer in
            value.withUnsafeBytes { buffer in
                setxattr(pathPointer, namePointer, buffer.baseAddress, value.count, 0, 0)
            }
        }
    }
    guard result == 0 else {
        throw ExportLivePhotoError.posix("写入 xattr \(name) 失败：\(path)", errno)
    }
}

func readXattr(path: String, name: String) throws -> [UInt8] {
    let size = path.withCString { pathPointer in
        name.withCString { namePointer in
            getxattr(pathPointer, namePointer, nil, 0, 0, 0)
        }
    }
    if size < 0 {
        throw ExportLivePhotoError.posix("读取 xattr \(name) 大小失败：\(path)", errno)
    }
    if size == 0 {
        return []
    }

    var value = [UInt8](repeating: 0, count: size)
    let valueCount = value.count
    let readSize = path.withCString { pathPointer in
        name.withCString { namePointer in
            value.withUnsafeMutableBytes { buffer in
                getxattr(pathPointer, namePointer, buffer.baseAddress, valueCount, 0, 0)
            }
        }
    }
    guard readSize >= 0 else {
        throw ExportLivePhotoError.posix("读取 xattr \(name) 失败：\(path)", errno)
    }
    return value
}

func listXattrNames(path: String) throws -> [String] {
    let size = path.withCString { pathPointer in
        listxattr(pathPointer, nil, 0, 0)
    }
    if size < 0 {
        throw ExportLivePhotoError.posix("列出 xattr 失败：\(path)", errno)
    }
    if size == 0 {
        return []
    }

    var buffer = [CChar](repeating: 0, count: size)
    let readSize = path.withCString { pathPointer in
        buffer.withUnsafeMutableBufferPointer { pointer in
            listxattr(pathPointer, pointer.baseAddress, pointer.count, 0)
        }
    }
    guard readSize >= 0 else {
        throw ExportLivePhotoError.posix("读取 xattr 列表失败：\(path)", errno)
    }

    var names: [String] = []
    var start = 0
    for index in 0..<buffer.count where buffer[index] == 0 {
        if index > start {
            let name = buffer[start..<index].withUnsafeBufferPointer { pointer in
                String(cString: pointer.baseAddress!)
            }
            names.append(name)
        }
        start = index + 1
    }
    return names
}

func copyXattrs(from src: URL, to dst: URL) throws {
    for name in try listXattrNames(path: src.path) {
        let value = try readXattr(path: src.path, name: name)
        try writeXattr(path: dst.path, name: name, value: value)
    }
}

func copyFileReplacingDestination(from src: URL, to dst: URL) throws {
    let manager = FileManager.default
    let parent = dst.deletingLastPathComponent()
    try manager.createDirectory(at: parent, withIntermediateDirectories: true)
    if manager.fileExists(atPath: dst.path) {
        try manager.removeItem(at: dst)
    }
    try manager.copyItem(at: src, to: dst)
}

func exportLivePhoto(stillSrc: URL, movSrc: URL, stillDst: URL, movDst: URL) throws {
    try copyFileReplacingDestination(from: stillSrc, to: stillDst)
    try copyFileReplacingDestination(from: movSrc, to: movDst)
    try copyXattrs(from: stillSrc, to: stillDst)
    try copyXattrs(from: movSrc, to: movDst)

    let heicsize = uint64LittleEndianBytes(try fileSize(stillDst))
    let stillMD5 = Array(try md5Hex(stillDst).utf8)
    try writeXattr(path: stillDst.path, name: "heicsize", value: heicsize)
    try writeXattr(path: movDst.path, name: "heicsize", value: heicsize)
    try writeXattr(path: stillDst.path, name: "md5", value: stillMD5)
    try writeXattr(path: stillDst.path, name: "fileattr", value: [0x10, 0x00, 0x00, 0x00])
    try writeXattr(path: movDst.path, name: "fileattr", value: [0x01, 0x00, 0x00, 0x00])
    try writeXattr(path: stillDst.path, name: "livephoto", value: nulTerminatedUTF8(movDst.lastPathComponent))
}

func main() throws {
    let args = CommandLine.arguments
    guard args.count == 5 else {
        throw ExportLivePhotoError.message("用法: helper <still-src> <mov-src> <still-dst> <mov-dst>")
    }

    try exportLivePhoto(
        stillSrc: URL(fileURLWithPath: args[1]),
        movSrc: URL(fileURLWithPath: args[2]),
        stillDst: URL(fileURLWithPath: args[3]),
        movDst: URL(fileURLWithPath: args[4])
    )
}

do {
    try main()
} catch {
    fputs("\(error)\n", stderr)
    exit(1)
}
