//! Tempo's macOS desktop shell.
//!
//! The shell is deliberately thin: it
//!   1. starts the frozen `tempo-server` sidecar on one of the OAuth-registered ports,
//!   2. shows a splash window while the server boots, then points the webview straight at
//!      `http://localhost:<port>` — the UI already speaks to the API with relative URLs, so
//!      loading it same-origin means the web frontend needs no changes at all,
//!   3. pushes any off-site navigation (i.e. Google's OAuth consent screen) out to the
//!      system browser, because Google refuses to render it inside an embedded webview,
//!   4. kills the sidecar on exit so no orphaned server survives the app.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::File;
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder};

/// Must stay in sync with `config.CANDIDATE_PORTS` on the Python side. Google validates
/// `redirect_uri` against an exact registered string, so the app can only use ports that are
/// registered on the OAuth client — it cannot grab an arbitrary free port.
const CANDIDATE_PORTS: [u16; 4] = [8000, 8317, 8318, 8319];

/// Generous: the first launch of a PyInstaller bundle pays a cold-start page-in cost, and a
/// slow disk shouldn't look like a crash.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(45);

struct Sidecar(Mutex<Option<Child>>);

fn pick_port() -> Option<u16> {
    CANDIDATE_PORTS
        .iter()
        .copied()
        .find(|p| TcpListener::bind(("127.0.0.1", *p)).is_ok())
}

/// Mirrors `config.STATE_DIR` on the Python side so both agree on where logs and settings go.
fn state_dir() -> PathBuf {
    if let Ok(d) = std::env::var("TEMPO_STATE_DIR") {
        return PathBuf::from(d);
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    PathBuf::from(home).join(".tempo")
}

/// Resolution order: env override → the bundled sidecar in `Contents/Resources/` → the
/// PyInstaller output in the repo (so `cargo run` works without building a .app).
fn server_bin() -> PathBuf {
    if let Ok(p) = std::env::var("TEMPO_SERVER_BIN") {
        return PathBuf::from(p);
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(macos_dir) = exe.parent() {
            // Contents/MacOS/Tempo → Contents/Resources/sidecar/tempo-server
            if let Some(contents) = macos_dir.parent() {
                let bundled = contents
                    .join("Resources")
                    .join("sidecar")
                    .join("tempo-server");
                if bundled.exists() {
                    return bundled;
                }
            }
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../packaging/dist/tempo-server/tempo-server")
}

/// The sidecar's stdout/stderr. A GUI app has no console, and losing the server's logs makes
/// field reports undebuggable — keep one file per launch, previous run as `.old`.
fn log_file() -> Option<File> {
    let dir = state_dir().join("logs");
    std::fs::create_dir_all(&dir).ok()?;
    let path = dir.join("tempo-server.log");
    if path.exists() {
        let _ = std::fs::rename(&path, dir.join("tempo-server.log.old"));
    }
    File::create(&path).ok()
}

/// Block until the sidecar accepts connections, or give up. A successful TCP connect is a good
/// enough readiness signal: uvicorn binds the socket only once the app is importable.
fn wait_for_server(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let addr = match ("127.0.0.1", port).to_socket_addrs() {
        Ok(mut a) => match a.next() {
            Some(a) => a,
            None => return false,
        },
        Err(_) => return false,
    };
    while Instant::now() < deadline {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    false
}

fn main() {
    let port = match pick_port() {
        Some(p) => p,
        None => {
            eprintln!("[tempo] no free port among {CANDIDATE_PORTS:?}");
            std::process::exit(1);
        }
    };
    let base_url = format!("http://localhost:{port}");

    tauri::Builder::default()
        .setup(move |app| {
            // 1. Start the Python sidecar on the port we reserved.
            let mut cmd = Command::new(server_bin());
            cmd.args(["--host", "127.0.0.1", "--port", &port.to_string()])
                // Tells the server this is the desktop shell, so the OAuth callback renders a
                // "return to Tempo" page instead of redirecting a stray browser tab into the app.
                .env("TEMPO_DESKTOP", "1")
                .env("TEMPO_PORT", port.to_string())
                .stdin(Stdio::null());
            match log_file() {
                Some(log) => match log.try_clone() {
                    Ok(err) => {
                        cmd.stdout(Stdio::from(log)).stderr(Stdio::from(err));
                    }
                    Err(_) => {
                        cmd.stdout(Stdio::from(log)).stderr(Stdio::null());
                    }
                },
                None => {
                    cmd.stdout(Stdio::null()).stderr(Stdio::null());
                }
            }
            let child = match cmd.spawn() {
                Ok(c) => Some(c),
                Err(e) => {
                    eprintln!("[tempo] failed to start sidecar: {e}");
                    None
                }
            };
            app.manage(Sidecar(Mutex::new(child)));

            // 2. Splash window. It stays up only until the server answers.
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Tempo")
                .inner_size(1100.0, 760.0)
                .min_inner_size(880.0, 600.0)
                .on_navigation(|url| {
                    // Same-origin (the sidecar) and the bundled splash stay in the window.
                    let local = matches!(url.host_str(), Some("localhost") | Some("127.0.0.1"));
                    if url.scheme() == "tauri" || local {
                        return true;
                    }
                    // Everything else — in practice Google's consent screen — goes to the
                    // system browser. Google returns `disallowed_useragent` for OAuth inside
                    // an embedded webview, so this is required, not just good manners.
                    let _ = Command::new("/usr/bin/open").arg(url.as_str()).spawn();
                    false
                })
                .build()?;

            // 3. Swap the splash for the real UI once the sidecar is listening. Off the main
            //    thread so the window paints while we wait.
            let handle = app.handle().clone();
            let url = base_url.clone();
            std::thread::spawn(move || {
                if wait_for_server(port, STARTUP_TIMEOUT) {
                    if let Ok(parsed) = Url::parse(&url) {
                        let _ = window.navigate(parsed);
                    }
                } else if let Some(w) = handle.get_webview_window("main") {
                    let _ = w.eval("document.body.classList.add('failed')");
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to start Tempo")
        .run(|app, event| {
            // Orphaned servers hold the port and would block the next launch.
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                if let Some(sidecar) = app.try_state::<Sidecar>() {
                    if let Ok(mut guard) = sidecar.0.lock() {
                        if let Some(child) = guard.as_mut() {
                            let _ = child.kill();
                            let _ = child.wait();
                        }
                    }
                }
            }
        });
}
