import Photos
import UIKit

/// One library item as loupe-web's PUT /sync/v1/items reads it (`loupe.sync_phone.PhoneItem`).
struct PhoneItem: Encodable, Sendable {
    let uuid: String
    let takenAt: Date?
    let mediaType: String
    let durationSeconds: Double?
    let isScreenshot: Bool
    let isScreenRecording: Bool
    let isBurst: Bool
    let albums: [String]

    enum CodingKeys: String, CodingKey {
        case uuid, albums
        case takenAt = "taken_at"
        case mediaType = "media_type"
        case durationSeconds = "duration_seconds"
        case isScreenshot = "is_screenshot"
        case isScreenRecording = "is_screen_recording"
        case isBurst = "is_burst"
    }
}

struct LibrarySnapshot: Sendable {
    let items: [PhoneItem]
    /// UUID to PhotoKit local identifier, for pulling previews.
    let identifiers: [String: String]
}

/// Reads Photos through PhotoKit. Read-only: nothing here changes the library.
/// Hidden items are left out of every fetch, and recently deleted items are never in one.
enum PhotoLibrary {
    static let previewEdge: CGFloat = 512
    static let jpegQuality: CGFloat = 0.85
    static let maxAlbumNameLength = 255
    static let maxAlbumsPerItem = 200
    static let maxDurationSeconds: Double = 24 * 3600
    private static let screenRecordingSubtype = PHAssetMediaSubtype(rawValue: 1 << 19)

    static var status: PHAuthorizationStatus { PHPhotoLibrary.authorizationStatus(for: .readWrite) }

    static func canRead(_ status: PHAuthorizationStatus) -> Bool {
        status == .authorized || status == .limited
    }

    static func requestAccess() async -> PHAuthorizationStatus {
        await PHPhotoLibrary.requestAuthorization(for: .readWrite)
    }

    static func uuid(fromLocalIdentifier identifier: String) -> String {
        String(identifier.split(separator: "/").first ?? Substring(identifier)).uppercased()
    }

    /// The same rule as `loupe.names.validate_album_name`, so one odd title can't fail the upload.
    static func albumName(_ title: String?) -> String? {
        guard let name = title?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty,
              name.unicodeScalars.count <= maxAlbumNameLength,
              name.unicodeScalars.allSatisfy(isPrintable) else { return nil }
        return name
    }

    /// Python's str.isprintable: no control, format, private, unassigned or separator scalars except " ".
    static func isPrintable(_ scalar: Unicode.Scalar) -> Bool {
        if scalar == " " { return true }
        switch scalar.properties.generalCategory {
        case .control, .format, .surrogate, .privateUse, .unassigned,
             .lineSeparator, .paragraphSeparator, .spaceSeparator:
            return false
        default:
            return true
        }
    }

    static func snapshot() -> LibrarySnapshot {
        let albums = albumMembers()
        let assets = PHAsset.fetchAssets(with: PHFetchOptions())
        var items: [PhoneItem] = []
        var identifiers: [String: String] = [:]
        items.reserveCapacity(assets.count)
        assets.enumerateObjects { asset, _, _ in
            guard let item = phoneItem(asset, albums: albums), identifiers[item.uuid] == nil else { return }
            identifiers[item.uuid] = asset.localIdentifier
            items.append(item)
        }
        items.sort { $0.uuid < $1.uuid }
        return LibrarySnapshot(items: items, identifiers: identifiers)
    }

    private static func phoneItem(_ asset: PHAsset, albums: [String: [String]]) -> PhoneItem? {
        let mediaType: String
        switch asset.mediaType {
        case .image: mediaType = "photo"
        case .video: mediaType = "video"
        default: return nil
        }
        let uuid = uuid(fromLocalIdentifier: asset.localIdentifier)
        let isVideo = asset.mediaType == .video
        return PhoneItem(
            uuid: uuid,
            takenAt: asset.creationDate,
            mediaType: mediaType,
            durationSeconds: isVideo ? min(max(asset.duration, 0), maxDurationSeconds) : nil,
            isScreenshot: asset.mediaSubtypes.contains(.photoScreenshot),
            isScreenRecording: asset.mediaSubtypes.contains(screenRecordingSubtype),
            isBurst: asset.burstIdentifier != nil,
            albums: Array((albums[uuid] ?? []).prefix(maxAlbumsPerItem)))
    }

    /// UUID to the titles of the user albums it is in, albums inside folders included.
    private static func albumMembers() -> [String: [String]] {
        var members: [String: [String]] = [:]
        let albums = PHAssetCollection.fetchAssetCollections(with: .album, subtype: .albumRegular, options: nil)
        albums.enumerateObjects { album, _, _ in
            guard let title = albumName(album.localizedTitle) else { return }
            PHAsset.fetchAssets(in: album, options: nil).enumerateObjects { asset, _, _ in
                let uuid = uuid(fromLocalIdentifier: asset.localIdentifier)
                if members[uuid]?.contains(title) != true { members[uuid, default: []].append(title) }
            }
        }
        return members
    }

    static func assets(for identifiers: [String]) -> [String: PHAsset] {
        var found: [String: PHAsset] = [:]
        PHAsset.fetchAssets(withLocalIdentifiers: identifiers, options: nil).enumerateObjects { asset, _, _ in
            found[uuid(fromLocalIdentifier: asset.localIdentifier)] = asset
        }
        return found
    }

    /// A JPEG at most `previewEdge` pixels on its long side. Photos may fetch a sized
    /// derivative from iCloud; it never downloads the original. Re-encoding drops EXIF and GPS.
    static func previewJPEG(_ asset: PHAsset) async -> Data? {
        let options = PHImageRequestOptions()
        options.deliveryMode = .highQualityFormat
        options.resizeMode = .fast
        options.isNetworkAccessAllowed = true
        options.isSynchronous = false
        let size = CGSize(width: previewEdge, height: previewEdge)
        let once = ResumeOnce()
        let image: UIImage? = await withCheckedContinuation { continuation in
            PHImageManager.default().requestImage(
                for: asset, targetSize: size, contentMode: .aspectFit, options: options
            ) { image, _ in
                if once.claim() { continuation.resume(returning: image) }
            }
        }
        return image?.jpegData(compressionQuality: jpegQuality)
    }
}

/// PhotoKit can call a handler more than once. The continuation must resume exactly once.
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }
}
