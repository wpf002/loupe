import SwiftUI

@main
struct LoupeApp: App {
    @State private var model = AppModel(config: .load())
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .task { await model.start() }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { model.refreshPhotos() }
                }
        }
    }
}

struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Group {
            if model.session == nil { SignInView() } else { HomeView() }
        }
        .background(Theme.paper.ignoresSafeArea())
        .tint(Theme.accent)
    }
}
