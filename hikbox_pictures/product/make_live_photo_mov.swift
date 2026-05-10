import Foundation
import AVFoundation
import CoreMedia
import CryptoKit
import Darwin

enum LivePhotoError: Error, CustomStringConvertible {
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

func metadataItem(identifier: AVMetadataIdentifier, value: NSCopying & NSObjectProtocol, dataType: String) -> AVMetadataItem {
    let item = AVMutableMetadataItem()
    item.identifier = identifier
    item.value = value
    item.dataType = dataType
    return item
}

func uint64LittleEndianBytes(_ value: UInt64) -> [UInt8] {
    var littleEndian = value.littleEndian
    return withUnsafeBytes(of: &littleEndian) { Array($0) }
}

func nulTerminatedUTF8(_ value: String) -> [UInt8] {
    Array(value.utf8) + [0]
}

func fileSize(_ url: URL) throws -> UInt64 {
    let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    guard let size = attrs[.size] as? NSNumber else {
        throw LivePhotoError.message("无法读取文件大小：\(url.path)")
    }
    return size.uint64Value
}

func modificationTimeSeconds(_ url: URL) throws -> UInt64 {
    let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    guard let date = attrs[.modificationDate] as? Date else {
        throw LivePhotoError.message("无法读取修改时间：\(url.path)")
    }
    return UInt64(date.timeIntervalSince1970)
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
        throw LivePhotoError.posix("写入 xattr \(name) 失败：\(path)", errno)
    }
}

func writeXattrs(path: String, values: [(String, [UInt8])]) throws {
    for (name, value) in values {
        try writeXattr(path: path, name: name, value: value)
    }
}

func writeLivePhotoXattrs(stillURL: URL, mp4URL: URL, movURL: URL) throws {
    let heicsize = uint64LittleEndianBytes(try fileSize(stillURL))
    let appmtime = uint64LittleEndianBytes(try modificationTimeSeconds(mp4URL))
    let addtime = uint64LittleEndianBytes(UInt64(Date().timeIntervalSince1970))
    let stillMD5 = Array(try md5Hex(stillURL).utf8)

    try writeXattrs(
        path: stillURL.path,
        values: [
            ("tmpsuf", []),
            ("heicsize", heicsize),
            ("appmtime", appmtime),
            ("md5", stillMD5),
            ("pictag", [0x9d, 0xa1, 0x27, 0x9b, 0x9b, 0x01, 0x00, 0x00]),
            ("address", nulTerminatedUTF8("(null)")),
            ("addtime", addtime),
            ("fileattr", [0x10, 0x00, 0x00, 0x00]),
            ("livephoto", nulTerminatedUTF8(movURL.lastPathComponent)),
        ]
    )

    try writeXattrs(
        path: movURL.path,
        values: [
            ("tmpsuf", []),
            ("heicsize", heicsize),
            ("appmtime", appmtime),
            ("fileattr", [0x01, 0x00, 0x00, 0x00]),
        ]
    )
}

func stillImageTimeFormatDescription() throws -> CMMetadataFormatDescription {
    let spec: [String: Any] = [
        kCMMetadataFormatDescriptionMetadataSpecificationKey_Identifier as String: "mdta/com.apple.quicktime.still-image-time",
        kCMMetadataFormatDescriptionMetadataSpecificationKey_DataType as String: kCMMetadataBaseDataType_SInt8 as String
    ]
    var description: CMFormatDescription?
    let status = CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
        allocator: kCFAllocatorDefault,
        metadataType: kCMMetadataFormatType_Boxed,
        metadataSpecifications: [spec] as CFArray,
        formatDescriptionOut: &description
    )
    guard status == noErr, let typed = description else {
        throw LivePhotoError.message("无法创建 still-image-time metadata 描述: \(status)")
    }
    return typed
}

func stillImageTimeGroup() -> AVTimedMetadataGroup {
    let item = AVMutableMetadataItem()
    item.identifier = AVMetadataIdentifier(rawValue: "mdta/com.apple.quicktime.still-image-time")
    item.keySpace = .quickTimeMetadata
    item.key = "com.apple.quicktime.still-image-time" as NSString
    item.value = NSNumber(value: 0)
    item.dataType = kCMMetadataBaseDataType_SInt8 as String
    return AVTimedMetadataGroup(
        items: [item],
        timeRange: CMTimeRange(start: .zero, duration: CMTime(value: 1, timescale: 600))
    )
}

func copyTrackPairs(asset: AVURLAsset, reader: AVAssetReader, writer: AVAssetWriter) throws -> [(AVAssetReaderTrackOutput, AVAssetWriterInput)] {
    let tracks = asset.tracks(withMediaType: .video) + asset.tracks(withMediaType: .audio)
    guard !tracks.isEmpty else {
        throw LivePhotoError.message("输入视频没有可复制的视频或音频轨道")
    }

    var pairs: [(AVAssetReaderTrackOutput, AVAssetWriterInput)] = []
    for track in tracks {
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: nil)
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else {
            throw LivePhotoError.message("无法添加 reader output: \(track.mediaType.rawValue)")
        }
        reader.add(output)

        let hint = track.formatDescriptions.first as! CMFormatDescription?
        let input = AVAssetWriterInput(mediaType: track.mediaType, outputSettings: nil, sourceFormatHint: hint)
        input.expectsMediaDataInRealTime = false
        if track.mediaType == .video {
            input.transform = track.preferredTransform
        }
        guard writer.canAdd(input) else {
            throw LivePhotoError.message("无法添加 writer input: \(track.mediaType.rawValue)")
        }
        writer.add(input)
        pairs.append((output, input))
    }
    return pairs
}

func makeMOV(inputURL: URL, outputURL: URL, assetIdentifier: String) throws {
    let asset = AVURLAsset(url: inputURL)
    let reader = try AVAssetReader(asset: asset)
    let writer = try AVAssetWriter(outputURL: outputURL, fileType: .mov)

    writer.metadata = [
        metadataItem(
            identifier: .quickTimeMetadataContentIdentifier,
            value: assetIdentifier as NSString,
            dataType: kCMMetadataBaseDataType_UTF8 as String
        ),
        metadataItem(
            identifier: .quickTimeMetadataLivePhotoVitalityScore,
            value: NSNumber(value: 1.0),
            dataType: kCMMetadataBaseDataType_Float32 as String
        ),
        metadataItem(
            identifier: .quickTimeMetadataLivePhotoVitalityScoringVersion,
            value: NSNumber(value: 0),
            dataType: kCMMetadataBaseDataType_SInt8 as String
        )
    ]

    let pairs = try copyTrackPairs(asset: asset, reader: reader, writer: writer)

    let metadataInput = AVAssetWriterInput(
        mediaType: .metadata,
        outputSettings: nil,
        sourceFormatHint: try stillImageTimeFormatDescription()
    )
    metadataInput.expectsMediaDataInRealTime = false
    guard writer.canAdd(metadataInput) else {
        throw LivePhotoError.message("无法添加 still-image-time metadata 轨道")
    }
    writer.add(metadataInput)
    let metadataAdaptor = AVAssetWriterInputMetadataAdaptor(assetWriterInput: metadataInput)

    guard writer.startWriting() else {
        throw LivePhotoError.message("MOV writer 启动失败: \(writer.error?.localizedDescription ?? "unknown")")
    }
    guard reader.startReading() else {
        throw LivePhotoError.message("MOV reader 启动失败: \(reader.error?.localizedDescription ?? "unknown")")
    }
    writer.startSession(atSourceTime: .zero)

    let group = DispatchGroup()
    let queue = DispatchQueue(label: "live-photo-track-copy")

    for (output, input) in pairs {
        group.enter()
        input.requestMediaDataWhenReady(on: queue) {
            while input.isReadyForMoreMediaData {
                if let sampleBuffer = output.copyNextSampleBuffer() {
                    if !input.append(sampleBuffer) {
                        input.markAsFinished()
                        group.leave()
                        return
                    }
                } else {
                    input.markAsFinished()
                    group.leave()
                    return
                }
            }
        }
    }

    group.enter()
    metadataInput.requestMediaDataWhenReady(on: queue) {
        if metadataInput.isReadyForMoreMediaData {
            _ = metadataAdaptor.append(stillImageTimeGroup())
            metadataInput.markAsFinished()
            group.leave()
        }
    }

    group.wait()

    if reader.status == .failed || reader.status == .cancelled {
        writer.cancelWriting()
        throw LivePhotoError.message("MOV reader 失败: \(reader.error?.localizedDescription ?? "unknown")")
    }

    let semaphore = DispatchSemaphore(value: 0)
    writer.finishWriting {
        semaphore.signal()
    }
    semaphore.wait()

    guard writer.status == .completed else {
        throw LivePhotoError.message("MOV writer 失败: \(writer.error?.localizedDescription ?? "unknown")")
    }
}

func main() throws {
    let args = CommandLine.arguments
    guard args.count == 5 else {
        throw LivePhotoError.message("用法: helper <jpg> <mp4> <mov> <asset-id>")
    }

    let stillURL = URL(fileURLWithPath: args[1])
    let mp4URL = URL(fileURLWithPath: args[2])
    let movURL = URL(fileURLWithPath: args[3])
    let assetIdentifier = args[4]

    try makeMOV(inputURL: mp4URL, outputURL: movURL, assetIdentifier: assetIdentifier)
    try writeLivePhotoXattrs(stillURL: stillURL, mp4URL: mp4URL, movURL: movURL)
}

do {
    try main()
} catch {
    fputs("\(error)\n", stderr)
    exit(1)
}
