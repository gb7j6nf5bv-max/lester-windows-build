//! Lester.exe: the file people double-click on Windows. It only starts app\setup_win.ps1
//! (hidden, no console window), which installs and opens Lester. No dependencies.
#![windows_subsystem = "windows"]

#[cfg(windows)]
fn main() {
    use std::os::windows::process::CommandExt;
    use std::process::Command;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    let script = std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().map(|dir| dir.join("app").join("setup_win.ps1")));
    let script = match script {
        Some(s) if s.exists() => s,
        _ => return message("Lester's files are missing. Unzip the whole Lester folder, then open Lester.exe again."),
    };
    let started = Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File"])
        .arg(&script)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn();
    if started.is_err() {
        message("Lester couldn't start Windows PowerShell, which it needs to run.");
    }
}

#[cfg(windows)]
fn message(text: &str) {
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(hwnd: *mut core::ffi::c_void, text: *const u16, caption: *const u16, kind: u32) -> i32;
    }
    let wide = |s: &str| s.encode_utf16().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let (t, c) = (wide(text), wide("Lester couldn't start"));
    unsafe { MessageBoxW(core::ptr::null_mut(), t.as_ptr(), c.as_ptr(), 0x10) };
}

#[cfg(not(windows))]
fn main() {
    eprintln!("Lester.exe is the Windows starter. On a Mac, open Lester.app.");
}
