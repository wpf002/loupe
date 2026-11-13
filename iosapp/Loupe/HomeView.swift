import SwiftUI
import UIKit

/// The full Loupe web app under a slim native bar that shows the Photos sync.
struct HomeView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: 0) {
            bar
            if let fraction = model.sync.fraction {
                ProgressView(value: fraction).progressViewStyle(.linear).tint(Theme.accent)
            }
            Rectangle().fill(Theme.line).frame(height: 1)
            LoupeWebView(url: model.config.webURL, revision: model.webRevision)
        }
    }

    private var bar: some View {
        HStack(spacing: Theme.space3) {
            status
            Spacer(minLength: Theme.space2)
            if model.photosOff {
                Button("Allow Photos") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                }
                .font(.subheadline.weight(.medium))
            } else {
                Button {
                    model.startSync()
                } label: {
                    Image(systemName: "arrow.triangle.2.circlepath")
                }
                .disabled(model.isSyncing)
                .accessibilityLabel("Sync")
            }
            Menu {
                if let email = model.session?.email { Text(email) }
                Button("Sign Out", role: .destructive) { Task { await model.signOut() } }
            } label: {
                Image(systemName: "person.crop.circle")
            }
            .accessibilityLabel("Account")
        }
        .font(.body)
        .padding(.horizontal, Theme.space4)
        .frame(minHeight: 44)
        .background(Theme.surface)
    }

    @ViewBuilder private var status: some View {
        HStack(spacing: Theme.space2) {
            if model.isSyncing { ProgressView().controlSize(.small) }
            Text(statusText)
                .font(.subheadline)
                .foregroundStyle(statusColor)
                .lineLimit(1)
                .truncationMode(.tail)
        }
    }

    private var statusText: String {
        if model.photosOff { return "Photos Access Off" }
        if case .idle = model.sync { return "Loupe" }
        return model.sync.label
    }

    private var statusColor: Color {
        if model.photosOff { return Theme.warn }
        if case .failed = model.sync { return Theme.warn }
        return Theme.muted
    }
}
