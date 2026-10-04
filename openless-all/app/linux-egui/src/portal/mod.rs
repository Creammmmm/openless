//! Desktop Portal adapter for Wayland desktops using IBus or other input methods.
//!
//! A private GIO helper owns asynchronous portal dialogs and clipboard FD
//! transfers. Its lifetime is tied to this host, never to a UI window.

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use futures_util::future::BoxFuture;
use openless_core::{
    BackendError, BackendErrorCode, DictationContext, InsertOutcome, InsertWriteResult, SessionId,
    TextInserter, TextInsertionSession,
};
use serde_json::{json, Value};

use crate::LinuxLaunchIntent;

const HELPER: &str = include_str!("bridge.py");
type Replies = Arc<Mutex<HashMap<u64, mpsc::Sender<Result<Value, String>>>>>;

#[derive(Clone, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct PortalStatus {
    pub shortcuts: bool,
    pub input: bool,
    pub connecting: bool,
    pub message: String,
}

pub struct PortalBackend {
    child: Mutex<Child>,
    writer: Mutex<ChildStdin>,
    status: Arc<Mutex<PortalStatus>>,
    events: Arc<Mutex<Vec<LinuxLaunchIntent>>>,
    replies: Replies,
    next_id: AtomicU64,
    operation: Mutex<()>,
    reader: Option<std::thread::JoinHandle<()>>,
}

fn error(message: impl Into<String>) -> BackendError {
    BackendError::new(BackendErrorCode::Platform, message)
}

/// Explicit overrides are useful on desktops running both input frameworks.
/// An absent fcitx5 addon must never prevent an IBus user from opening settings.
pub fn use_portal_backend() -> Result<bool, BackendError> {
    match std::env::var("OPENLESS_INPUT_BACKEND").as_deref() {
        Ok("portal") => Ok(true),
        Ok("fcitx5") => Ok(false),
        Ok("auto") | Err(_) => Ok(!crate::fcitx5_available()),
        Ok(_) => Err(error(
            "OPENLESS_INPUT_BACKEND must be auto, portal or fcitx5",
        )),
    }
}

impl PortalBackend {
    /// Starting the helper never opens a permission dialog. The settings action
    /// calls connect explicitly after explaining what the desktop will request.
    pub fn start(data_dir: &std::path::Path) -> Result<Arc<Self>, BackendError> {
        Self::spawn_helper(data_dir, HELPER)
    }

    fn spawn_helper(data_dir: &std::path::Path, script: &str) -> Result<Arc<Self>, BackendError> {
        let mut child = Command::new("/usr/bin/python3")
            .args(["-u", "-c", script])
            .arg(data_dir.join("desktop-portal.json"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| {
                error(format!(
                    "Desktop input requires python3 and python3-gi: {e}"
                ))
            })?;
        let writer = child.stdin.take().expect("piped portal stdin");
        let output = child.stdout.take().expect("piped portal stdout");
        let status = Arc::new(Mutex::new(PortalStatus::default()));
        let events = Arc::new(Mutex::new(Vec::new()));
        let replies: Replies = Arc::default();
        let reader_status = Arc::clone(&status);
        let reader_events = Arc::clone(&events);
        let reader_replies = Arc::clone(&replies);
        let reader = std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let Ok(line) = line else { break };
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    continue;
                };
                if let Some(id) = value["id"].as_u64() {
                    if let Some(reply) = reader_replies.lock().unwrap().remove(&id) {
                        let result = match value["error"].as_str() {
                            Some(message) => Err(message.to_string()),
                            None => Ok(value),
                        };
                        let _ = reply.send(result);
                    }
                } else if value["event"] == "status" {
                    if let Ok(next) = serde_json::from_value::<PortalStatus>(value) {
                        *reader_status.lock().unwrap() = next;
                    }
                } else if value["event"] == "hotkey" {
                    let intent = match value["action"].as_str() {
                        Some("dictation") => Some(openless_core::CliIntent::ToggleDictation),
                        Some("cancel") => Some(openless_core::CliIntent::CancelDictation),
                        _ => None,
                    };
                    if let Some(intent) = intent {
                        reader_events
                            .lock()
                            .unwrap()
                            .push(LinuxLaunchIntent::Cli(intent));
                    }
                }
            }
            *reader_status.lock().unwrap() = PortalStatus {
                message: "Desktop helper stopped. Check python3-gi and restart OpenLess.".into(),
                ..Default::default()
            };
            for (_, reply) in reader_replies.lock().unwrap().drain() {
                let _ = reply.send(Err("Desktop helper stopped".into()));
            }
        });
        Ok(Arc::new(Self {
            child: Mutex::new(child),
            writer: Mutex::new(writer),
            status,
            events,
            replies,
            next_id: AtomicU64::new(1),
            operation: Mutex::new(()),
            reader: Some(reader),
        }))
    }

    #[cfg(test)]
    pub(crate) fn fixture() -> Arc<Self> {
        Self::spawn_helper(
            std::path::Path::new("/tmp"),
            "import sys\nfor line in sys.stdin:\n pass\n",
        )
        .unwrap()
    }

    pub fn status(&self) -> PortalStatus {
        self.status.lock().unwrap().clone()
    }

    pub fn connect(&self) -> Result<(), BackendError> {
        self.send(json!({"op": "connect"}))
    }

    pub fn configure_shortcuts(&self) -> Result<(), BackendError> {
        self.send(json!({"op": "configure"}))
    }

    pub fn disconnect(&self) -> Result<(), BackendError> {
        self.send(json!({"op": "disconnect"}))
    }

    pub fn drain(&self, apply: impl FnMut(LinuxLaunchIntent)) {
        self.events.lock().unwrap().drain(..).for_each(apply);
    }

    fn send(&self, value: Value) -> Result<(), BackendError> {
        let encoded = serde_json::to_vec(&value).map_err(|e| error(e.to_string()))?;
        if encoded.len() >= 4 * 1024 * 1024 {
            return Err(error("Desktop input text exceeds the 4 MiB message limit"));
        }
        let mut writer = self.writer.lock().unwrap();
        writer
            .write_all(&encoded)
            .and_then(|_| writer.write_all(b"\n"))
            .and_then(|_| writer.flush())
            .map_err(|e| error(e.to_string()))
    }

    fn request(&self, mut value: Value) -> Result<InsertOutcome, BackendError> {
        let _operation = self.operation.lock().unwrap();
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        value["id"] = json!(id);
        let (tx, rx) = mpsc::channel();
        self.replies.lock().unwrap().insert(id, tx);
        if let Err(error) = self.send(value) {
            self.replies.lock().unwrap().remove(&id);
            return Err(error);
        }
        match rx.recv_timeout(Duration::from_secs(25)) {
            Ok(Ok(value)) => serde_json::from_value(value["outcome"].clone())
                .map_err(|e| error(format!("Invalid desktop response: {e}"))),
            Ok(Err(message)) => Err(error(message)),
            Err(_) => {
                self.replies.lock().unwrap().remove(&id);
                // Never replay uncertain keyboard operations after timeout.
                let _ = self.disconnect();
                Err(error("Desktop input timed out; reconnect in Settings"))
            }
        }
    }

    pub fn copy_text(&self, text: &str) -> Result<(), BackendError> {
        self.request(json!({"op": "copy", "text": text}))
            .map(|_| ())
    }
}

impl Drop for PortalBackend {
    fn drop(&mut self) {
        let child = self.child.get_mut().unwrap();
        let _ = child.kill();
        let _ = child.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

pub struct PortalTextInserter(pub Arc<PortalBackend>);

impl TextInserter for PortalTextInserter {
    fn begin(
        &self,
        session_id: SessionId,
        context: Arc<DictationContext>,
    ) -> BoxFuture<'static, Result<Arc<dyn TextInsertionSession>, BackendError>> {
        let session = PortalInsertionSession {
            backend: Arc::clone(&self.0),
            session: session_id.to_string(),
            shortcut: serde_json::to_value(context.insertion.paste_shortcut).unwrap(),
            finished: AtomicBool::new(false),
            cancelled: Arc::new(AtomicBool::new(false)),
        };
        Box::pin(async move { Ok(Arc::new(session) as Arc<dyn TextInsertionSession>) })
    }
}

struct PortalInsertionSession {
    backend: Arc<PortalBackend>,
    session: String,
    shortcut: Value,
    finished: AtomicBool,
    cancelled: Arc<AtomicBool>,
}

impl TextInsertionSession for PortalInsertionSession {
    fn supports_streaming(&self) -> bool {
        false
    }

    fn write(&self, _text: String) -> BoxFuture<'static, Result<InsertWriteResult, BackendError>> {
        Box::pin(async {
            Err(BackendError::new(
                BackendErrorCode::Unsupported,
                "Desktop paste requires the complete result",
            ))
        })
    }

    fn copy(&self, text: String) -> BoxFuture<'static, Result<(), BackendError>> {
        let backend = Arc::clone(&self.backend);
        Box::pin(async move {
            tokio::task::spawn_blocking(move || backend.copy_text(&text))
                .await
                .map_err(|e| error(e.to_string()))?
        })
    }

    fn finish(&self, text: String) -> BoxFuture<'static, Result<InsertOutcome, BackendError>> {
        if self.finished.swap(true, Ordering::AcqRel) {
            return Box::pin(async { Err(error("Insertion has already finished")) });
        }
        if text.is_empty() {
            return Box::pin(async { Ok(InsertOutcome::CopiedFallback) });
        }
        let backend = Arc::clone(&self.backend);
        let cancelled = Arc::clone(&self.cancelled);
        let command = json!({"op": "insert", "text": text, "session": self.session,
                             "shortcut": self.shortcut});
        Box::pin(async move {
            tokio::task::spawn_blocking(move || {
                if cancelled.load(Ordering::Acquire) {
                    return Err(BackendError::new(
                        BackendErrorCode::Cancelled,
                        "Insertion cancelled",
                    ));
                }
                backend.request(command)
            })
            .await
            .map_err(|e| error(e.to_string()))?
        })
    }

    fn cancel(&self) -> BoxFuture<'static, Result<(), BackendError>> {
        self.cancelled.store(true, Ordering::Release);
        let result = self
            .backend
            .send(json!({"op": "cancel", "session": self.session}));
        Box::pin(async move { result })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn responding_backend() -> Arc<PortalBackend> {
        PortalBackend::spawn_helper(
            std::path::Path::new("/tmp"),
            r#"import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' in request:
        print(json.dumps({'id': request['id'], 'outcome': 'pasteSent' if request['op'] == 'insert' else 'copiedFallback'}), flush=True)
"#,
        )
        .unwrap()
    }

    fn session(backend: Arc<PortalBackend>) -> PortalInsertionSession {
        PortalInsertionSession {
            backend,
            session: "test-session".into(),
            shortcut: json!("ctrlV"),
            finished: AtomicBool::new(false),
            cancelled: Arc::new(AtomicBool::new(false)),
        }
    }

    #[tokio::test]
    async fn complete_result_is_pasted_once_and_never_reported_as_verified_insertion() {
        let session = session(responding_backend());
        assert!(!session.supports_streaming());
        assert!(session.write("partial".into()).await.is_err());
        assert_eq!(
            session
                .finish("\u{4e2d}\u{6587} 🌍\nEnglish".into())
                .await
                .unwrap(),
            InsertOutcome::PasteSent,
        );
        assert!(session.finish("duplicate".into()).await.is_err());
    }

    #[tokio::test]
    async fn cancellation_before_finish_prevents_paste() {
        let session = session(responding_backend());
        session.cancel().await.unwrap();
        let error = session.finish("cancelled result".into()).await.unwrap_err();
        assert_eq!(error.code, BackendErrorCode::Cancelled);
    }

    #[test]
    fn oversized_request_is_rejected_and_pending_reply_is_removed() {
        let backend = responding_backend();
        assert!(backend
            .request(json!({"op": "copy", "text": "x".repeat(4 * 1024 * 1024)}))
            .is_err());
        assert!(backend.replies.lock().unwrap().is_empty());
        backend.copy_text("still usable").unwrap();
    }
}
