# 海康智存 Live Photo 机制记录

本文记录 HikBoxPictures 当前已验证的海康智存 iOS app Live Photo 识别机制，用于后续维护 JPG/MP4 转换和 HEIC/MOV 导出逻辑。

## 结论概要

海康智存识别 Live Photo 依赖两层信息：

1. 静态图和 motion 文件之间的文件系统扩展属性（xattr）关联。
2. 文件内部 metadata 中一致的 Live Photo content identifier。

只复制文件内容不够。当前 macOS Python 环境里 `shutil.copy2` 不可靠保留 xattr，因此导出 Live Photo 时必须显式复制并修正 xattr。

## 测试数据分工

`tests/fixtures/live-photo/` 固定包含两类真实样本：

- `input.jpg` + `input.mp4`：转换输入。它们不带海康智存 Live Photo 业务 xattr，也没有 Live Photo content identifier。
- `input2.HEIC` + `.input2.MOV`：导出输入。它们带 Live Photo 相关 xattr，并且内部 content identifier 匹配，当前使用合法 RFC 4122 UUID；已实测可被海康智存 app 显示为 Live 图并正常预览。它们用于验证导出代码是否保留和修正 metadata/xattr。

这两类样本用途不同，不要混用：

- 测试转换逻辑时，用 JPG/MP4 生成新的 JPG/MOV。
- 测试导出逻辑时，用 HEIC/MOV 验证复制、重命名和 xattr 修正。

## 静态图 xattr

海康智存可识别的静态图（HEIC/JPG）至少需要以下关键 xattr：

- `livephoto`：隐藏 MOV 文件名，UTF-8 字符串，以 `\0` 结尾，例如 `.input2.MOV\0`。
- `fileattr`：`10 00 00 00`，表示静态图。
- `heicsize`：8 字节 little-endian 的静态图文件大小。字段名即使用于 JPG 仍叫 `heicsize`。
- `md5`：静态图文件内容 MD5 的 ASCII 十六进制字符串。

实际海康智存样本里还会出现：

- `tmpsuf`
- `appmtime`
- `gps`
- `pictag`
- `address`
- `addtime`

其中 `appmtime` / `addtime` 是 8 字节 little-endian timestamp。

## MOV xattr

配套隐藏 MOV 需要以下关键 xattr：

- `fileattr`：`01 00 00 00`，表示 motion 文件。
- `heicsize`：8 字节 little-endian 的静态图文件大小，必须与静态图侧一致。

实际海康智存样本里还会出现：

- `tmpsuf`
- `appmtime`

MOV 文件自身不需要 `livephoto` xattr；关联方向在静态图的 `livephoto` 上。

## 内部 metadata

### HEIC/MOV 导出

已有 iPhone Live Photo 的 HEIC 和 MOV 内部 metadata 已经包含匹配的 content identifier。导出逻辑应按字节复制文件内容，使这些 metadata 原样保留，再显式复制并修正 xattr。

导出时如果 MOV 被重命名，必须更新静态图 xattr：

```text
livephoto=<导出后的隐藏 MOV 文件名>\0
```

同时导出结果必须重新计算：

- 静态图 `md5`
- 静态图与 MOV 的 `heicsize`
- 静态图 `fileattr=10 00 00 00`
- MOV `fileattr=01 00 00 00`

### JPG/MP4 转换

从普通 JPG/MP4 生成 Live Photo 时，单靠 xattr 不够。已验证海康智存还要求：

- JPG 内部 Apple MakerNote tag 17 写入 UUID。
- MOV 内部 `com.apple.quicktime.content.identifier` 写入同一个 UUID。

这个 UUID 必须是标准 RFC 4122 UUID。不要用 `33333333-4444-5555-6666-777777777777` 这类看起来像 UUID 但 variant 非 RFC 4122 的占位值；海康智存可能会拒绝这类 content identifier。脚本默认自动生成标准 v4 UUID，只有调试时才建议手工传 `--asset-id`。

JPG 内部可通过 ExifTool 写入 `MakerNoteApple` 二进制块。块内容包含 `Apple iOS` 标记，并在 Apple MakerNote tag `0x0011` 写入 content identifier。写入后必须验证 JPEG scan data 与输入 JPG 保持一致，避免重编码图像数据。

MOV 侧需要写入 QuickTime Live Photo metadata：

- `com.apple.quicktime.content.identifier`
- `live-photo.vitality-score`
- `live-photo.vitality-scoring-version`
- `still-image-time` metadata track

当前转换脚本用 Swift/AVFoundation 将 MP4 pass-through 封装为 MOV，并写入这些 metadata。

## 实现约束

### 转换 JPG/MP4

当前职责划分：

- Python 脚本负责复制 JPG、用 ExifTool 写 JPG MakerNote、调用 Swift helper。
- Swift helper 负责 MP4 到 MOV 封装、写 QuickTime metadata、写 JPG/MOV xattr。

依赖：

- Python 侧使用 `PyExifTool` 调用系统 `exiftool`。
- macOS 侧需要 `swift` 可执行文件。

### 导出 HEIC/MOV

导出逻辑必须使用包内 Swift helper 显式处理 xattr，不要依赖 `shutil.copy2`。

正确流程：

1. 复制静态图到目标路径。
2. 复制 MOV 到目标路径。
3. 复制源静态图全部 xattr 到目标静态图。
4. 复制源 MOV 全部 xattr 到目标 MOV。
5. 修正目标静态图 `livephoto` 指向目标 MOV 文件名。
6. 修正目标静态图和目标 MOV 的 `fileattr`。
7. 根据目标静态图重新写 `heicsize` 和 `md5`。

文件内容按字节复制，不应改写 HEIC/JPG/MOV 内部 metadata。

## 校验命令

查看 content identifier：

```bash
exiftool -G1 -s -ContentIdentifier <静态图> <MOV>
```

查看 xattr 名称：

```bash
xattr <文件>
```

查看 xattr 原始十六进制值：

```bash
xattr -px livephoto <静态图>
xattr -px fileattr <静态图>
xattr -px fileattr <MOV>
xattr -px heicsize <静态图>
xattr -px heicsize <MOV>
xattr -px md5 <静态图>
```

关键期望：

```text
静态图 livephoto = <隐藏 MOV 文件名>\0
静态图 fileattr = 10 00 00 00
MOV fileattr = 01 00 00 00
静态图 heicsize = MOV heicsize = little-endian(静态图文件大小)
静态图 md5 = md5(静态图文件内容)
静态图 ContentIdentifier = MOV ContentIdentifier
```

## 已知非目标

- 不尝试让普通 JPG/PNG 在扫描阶段自动匹配 Live Photo MOV；扫描语义仍以现有产品规则为准。
- 不用 ImageIO 写 JPG MakerNote，因为它会重编码 JPG，不满足 scan data 不变的要求。
- 不依赖 macOS Python 的 `shutil.copy2` 保留 xattr。
