import SwiftUI

struct SignInView: View {
    @Environment(AppModel.self) private var model
    @State private var email = ""
    @State private var password = ""
    @State private var creating = false
    @State private var busy = false
    @State private var error: String?
    @FocusState private var focus: Field?

    private enum Field { case email, password }

    private var title: String { creating ? "Create Account" : "Sign In" }
    private var ready: Bool { !email.isEmpty && !password.isEmpty && !busy }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.space5) {
                LoupeMark()
                Text(title).font(Theme.display(.largeTitle)).foregroundStyle(Theme.ink)
                VStack(alignment: .leading, spacing: Theme.space4) {
                    field("Email") {
                        TextField("", text: $email)
                            .textContentType(.username)
                            .keyboardType(.emailAddress)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .focused($focus, equals: .email)
                            .submitLabel(.next)
                            .onSubmit { focus = .password }
                    }
                    field("Password") {
                        SecureField("", text: $password)
                            .textContentType(creating ? .newPassword : .password)
                            .focused($focus, equals: .password)
                            .submitLabel(.go)
                            .onSubmit(submit)
                    }
                }
                if let message = error ?? model.signInMessage {
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(Theme.warn)
                        .accessibilityAddTraits(.isStaticText)
                }
                VStack(spacing: Theme.space3) {
                    Button(action: submit) {
                        if busy { ProgressView().tint(Theme.accentInk) } else { Text(title) }
                    }
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(!ready)
                    .opacity(ready ? 1 : 0.6)
                    Button(creating ? "Sign In" : "Create Account") {
                        creating.toggle()
                        error = nil
                    }
                    .buttonStyle(SecondaryButtonStyle())
                    .disabled(busy)
                }
            }
            .padding(Theme.space5)
            .frame(maxWidth: Theme.formWidth)
            .frame(maxWidth: .infinity)
            .padding(.top, Theme.space6)
        }
        .scrollDismissesKeyboard(.interactively)
        .onAppear { focus = .email }
    }

    private func field(_ label: String, @ViewBuilder input: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: Theme.space1) {
            Text(label).font(.subheadline.weight(.medium)).foregroundStyle(Theme.muted)
            input()
                .padding(.horizontal, Theme.space3)
                .frame(minHeight: 44)
                .background(Theme.surface, in: RoundedRectangle(cornerRadius: Theme.radiusM))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusM).strokeBorder(Theme.line))
                .foregroundStyle(Theme.ink)
        }
    }

    private func submit() {
        guard ready else { return }
        busy = true
        error = nil
        Task {
            defer { busy = false }
            do {
                try await model.signIn(email: email, password: password, create: creating)
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
