//! lester-sideload: signs Lester for iPhone with the user's own Apple ID and installs it over USB.
//!
//! Talks to Lester (the Mac app) with one JSON object per line.
//! First line on stdin:  {"appleId": "...", "password": "...", "udid": "...", "ipa": "/path/Lester-iPhone.ipa"}
//!   The password arrives on stdin (never as an argument, so it can't be seen in the process list),
//!   goes only to Apple's sign-in, and is never written anywhere.
//! Events on stdout: {"event": "status" | "2fa" | "certs" | "progress" | "done" | "error", ...}
//! Answers on stdin:  {"code": "123456"} | {"action": "resend" | "devices" | "abort"} | {"action": "sms", "id": 1}
//!                    {"revoke": ["serial", ...]}  (empty list = cancel)
//!
//! Built on isideload (MIT, github.com/nab138/isideload). Its sign-in helper server (anisette) only
//! receives a virtual device ID, never the Apple ID or password.

use std::{io::BufRead, path::PathBuf, sync::Arc};

use idevice::usbmuxd::{UsbmuxdAddr, UsbmuxdConnection};
use isideload::{
    anisette::remote_v3::RemoteV3AnisetteProvider,
    auth::apple_account::{AppleAccount, TwoFactorCallbackParams, TwoFactorCallbackResponse},
    dev::{certificates::DevelopmentCertificate, developer_session::DeveloperSession},
    sideload::{SideloaderBuilder, TeamSelection, builder::MaxCertsBehavior},
    util::{callbacks::ExtensionsBehaviorCallbackBox, keyring_storage::KeyringStorage},
};
use serde_json::{Value, json};
use tokio::sync::{Mutex, mpsc};

fn emit(v: Value) {
    println!("{v}");
}

fn fail(text: impl std::fmt::Display) -> ! {
    emit(json!({"event": "error", "text": text.to_string()}));
    std::process::exit(1);
}

type Inbox = Arc<Mutex<mpsc::UnboundedReceiver<Value>>>;

async fn next_answer(inbox: &Inbox) -> Value {
    inbox.lock().await.recv().await.unwrap_or(json!({"action": "abort"}))
}

#[tokio::main]
async fn main() {
    rustls::crypto::ring::default_provider().install_default().ok();
    if let Err(e) = isideload::init() {
        fail(format!("Couldn't start: {e}"));
    }

    // stdin -> channel, one JSON object per line
    let (tx, rx) = mpsc::unbounded_channel::<Value>();
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines().map_while(Result::ok) {
            if let Ok(v) = serde_json::from_str::<Value>(&line) {
                if tx.send(v).is_err() {
                    break;
                }
            }
        }
    });
    let inbox: Inbox = Arc::new(Mutex::new(rx));

    let first = next_answer(&inbox).await;
    let field = |k: &str| first.get(k).and_then(Value::as_str).map(str::to_string);
    let (Some(apple_id), Some(password), Some(udid), Some(ipa)) =
        (field("appleId"), field("password"), field("udid"), field("ipa"))
    else {
        fail("Missing Apple ID, password, iPhone or app file");
    };

    emit(json!({"event": "status", "text": "Signing in to Apple…"}));
    let two_factor_inbox = inbox.clone();
    let get_2fa_code = move |params: TwoFactorCallbackParams| {
        let inbox = two_factor_inbox.clone();
        async move {
            let numbers: Vec<Value> = params
                .numbers
                .iter()
                .map(|n| json!({"id": n.id, "number": n.number_with_dial_code}))
                .collect();
            emit(json!({"event": "2fa", "sms": params.sms, "unknown": params.unknown,
                        "numbers": numbers, "selected": params.selected_number_id,
                        "lastError": params.last_error}));
            let answer = next_answer(&inbox).await;
            if let Some(code) = answer.get("code").and_then(Value::as_str) {
                return Ok(TwoFactorCallbackResponse::SubmitCode(code.trim().to_string()));
            }
            Ok(match answer.get("action").and_then(Value::as_str) {
                Some("resend") => TwoFactorCallbackResponse::ResendCode,
                Some("devices") => TwoFactorCallbackResponse::SendToDevices,
                Some("sms") => match answer.get("id").and_then(Value::as_u64) {
                    Some(id) => TwoFactorCallbackResponse::SendSms(id as u32),
                    None => TwoFactorCallbackResponse::Abort,
                },
                _ => TwoFactorCallbackResponse::Abort,
            })
        }
    };

    let anisette = match RemoteV3AnisetteProvider::default() {
        Ok(p) => p.set_serial_number("2".to_string()),
        Err(e) => fail(format!("Couldn't reach Apple's sign-in helper: {e}")),
    };
    let mut account = match AppleAccount::builder(&apple_id).anisette_provider(anisette).login(&password, get_2fa_code).await {
        Ok(a) => a,
        Err(e) => fail(format!("Apple sign-in failed: {e}")),
    };
    drop(password);

    emit(json!({"event": "status", "text": "Getting your free developer certificate…"}));
    let session = match DeveloperSession::from_account(&mut account).await {
        Ok(s) => s,
        Err(e) => fail(format!("Couldn't open your developer account: {e}")),
    };

    let mut usbmuxd = match UsbmuxdConnection::default().await {
        Ok(u) => u,
        Err(e) => fail(format!("Couldn't talk to the iPhone: {e}")),
    };
    let devices = usbmuxd.get_devices().await.unwrap_or_default();
    let Some(device) = devices.iter().find(|d| d.udid == udid) else {
        fail("The iPhone isn't connected. Plug it in with a cable and unlock it.");
    };
    let addr = UsbmuxdAddr::from_env_var().unwrap_or_default();
    let provider = device.to_provider(addr, "Lester");

    // Free Apple IDs can only have a couple of certificates. If they're used up, ask which to replace.
    let certs_inbox = inbox.clone();
    let choose_certs = move |certs: Vec<DevelopmentCertificate>| {
        let inbox = certs_inbox.clone();
        async move {
            let list: Vec<Value> = certs
                .iter()
                .map(|c| json!({"serial": c.serial_number, "name": c.name, "machine": c.machine_name}))
                .collect();
            emit(json!({"event": "certs", "certs": list}));
            let answer = next_answer(&inbox).await;
            let serials: Vec<String> = answer
                .get("revoke")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(|s| s.as_str().map(str::to_string)).collect())
                .unwrap_or_default();
            Ok(if serials.is_empty() { None } else { Some(serials) })
        }
    };

    let mut sideloader = SideloaderBuilder::<_, ExtensionsBehaviorCallbackBox>::new(session, apple_id.clone())
        .team_selection(TeamSelection::First)
        .max_certs_behavior(MaxCertsBehavior::Prompt(choose_certs))
        .storage(Box::new(KeyringStorage::new("Lester".to_string())))
        .machine_name("Lester".to_string())
        .build();

    emit(json!({"event": "status", "text": "Signing and installing Lester for iPhone…"}));
    let progress = |p: f32| {
        emit(json!({"event": "progress", "value": p}));
        std::future::ready(())
    };
    match sideloader.install_app(&provider, PathBuf::from(ipa), false, Some(progress), None).await {
        Ok(_) => emit(json!({"event": "done"})),
        Err(e) => fail(format!("Install failed: {e}")),
    }
}
