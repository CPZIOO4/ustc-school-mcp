fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["backend_request", "open_external", "browser_open", "browser_action"]),
        ),
    ).expect("native application configuration is invalid");
}
