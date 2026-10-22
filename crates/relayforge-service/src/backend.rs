use relayforge_core::{Method, MAX_MESSAGE};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    path::Path,
    process::Stdio,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{mpsc, oneshot},
    time::{timeout, Duration},
};

type Reply = Result<Value, String>;
type Pending = Arc<Mutex<HashMap<u64, oneshot::Sender<Reply>>>>;
pub type Event = (Value, oneshot::Sender<()>);

pub struct Worker {
    input: tokio::sync::Mutex<Option<ChildStdin>>,
    child: tokio::sync::Mutex<Child>,
    pending: Pending,
    next: AtomicU64,
}

pub async fn read_frame<R: AsyncBufRead + Unpin>(reader: &mut R) -> Result<Option<Value>, String> {
    let mut frame = Vec::new();
    loop {
        let buffer = reader.fill_buf().await.map_err(|_| "Worker read failed")?;
        if buffer.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err("Truncated worker frame".into())
            };
        }
        let length = buffer
            .iter()
            .position(|b| *b == b'\n')
            .map_or(buffer.len(), |n| n + 1);
        if frame.len() + length > MAX_MESSAGE {
            return Err("Worker frame exceeds limit".into());
        }
        frame.extend_from_slice(&buffer[..length]);
        reader.consume(length);
        if frame.last() == Some(&b'\n') {
            return serde_json::from_slice(&frame)
                .map(Some)
                .map_err(|_| "Malformed worker frame".into());
        }
    }
}

impl Worker {
    pub async fn spawn(root: &Path, events: mpsc::Sender<Event>) -> Result<Arc<Self>, String> {
        let mut child = Command::new("/usr/bin/python3")
            .arg("-I")
            .arg(root.join("compat/worker.py"))
            .arg(root.join("vendor/wifi-relay"))
            .env_remove("PYTHONPATH")
            .env_remove("PYTHONHOME")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| "Cannot start networking worker")?;
        let mut output = BufReader::new(child.stdout.take().ok_or("Missing worker output")?);
        let ready = timeout(Duration::from_secs(15), read_frame(&mut output))
            .await
            .map_err(|_| "Networking worker startup timed out")??;
        if ready != Some(json!({"ready":1})) {
            return Err("Invalid worker handshake".into());
        }
        let worker = Arc::new(Self {
            input: tokio::sync::Mutex::new(Some(child.stdin.take().ok_or("Missing worker input")?)),
            child: tokio::sync::Mutex::new(child),
            pending: Arc::new(Mutex::new(HashMap::new())),
            next: AtomicU64::new(1),
        });
        let pending = worker.pending.clone();
        tokio::spawn(async move {
            while let Ok(Some(value)) = read_frame(&mut output).await {
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    let sender = pending.lock().unwrap().remove(&id);
                    if let Some(sender) = sender {
                        let result = if value.get("error").is_some() {
                            // Errors are deliberately generic; never echo a malformed secret-bearing frame.
                            Err("Networking worker rejected the operation".into())
                        } else {
                            value
                                .get("result")
                                .cloned()
                                .ok_or_else(|| "Missing worker result".into())
                        };
                        let _ = sender.send(result);
                    }
                } else {
                    let (ack, processed) = oneshot::channel();
                    if events.send((value, ack)).await.is_err() || processed.await.is_err() {
                        break;
                    }
                }
            }
            for (_, sender) in pending.lock().unwrap().drain() {
                let _ = sender.send(Err("Networking worker disconnected".into()));
            }
            let (ack, _) = oneshot::channel();
            let _ = events.send((json!({"lost":true}), ack)).await;
        });
        Ok(worker)
    }

    pub async fn call(&self, method: Method, params: Value) -> Reply {
        if matches!(
            method,
            Method::GetConfig | Method::SetConfig | Method::GetInterfaces | Method::GetCapabilities
        ) {
            return Err("Method is implemented natively; worker delegation is forbidden".into());
        }
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (sender, receiver) = oneshot::channel();
        {
            let mut pending = self.pending.lock().unwrap();
            if pending.len() >= 32 {
                return Err("Too many pending operations".into());
            }
            pending.insert(id, sender);
        }
        let mut bytes =
            serde_json::to_vec(&json!({"id":id,"method":method.name(),"params":params})).unwrap();
        bytes.push(b'\n');
        let result = async {
            self.input
                .lock()
                .await
                .as_mut()
                .ok_or("Worker input closed".to_owned())?
                .write_all(&bytes)
                .await
                .map_err(|_| "Worker input closed".to_owned())?;
            receiver.await.map_err(|_| "Worker reply lost".to_owned())?
        };
        let result = timeout(Duration::from_secs(90), result)
            .await
            .unwrap_or_else(|_| Err("Worker operation timed out; cleanup unconfirmed".into()));
        self.pending.lock().unwrap().remove(&id);
        result
    }

    pub async fn shutdown(&self) -> Result<(), String> {
        let stopped = timeout(
            Duration::from_secs(50),
            self.call(Method::Stop, Value::Null),
        )
        .await;
        self.close_and_wait().await?;
        if matches!(stopped, Ok(Ok(Value::Bool(true)))) {
            Ok(())
        } else {
            Err("Backend cleanup did not confirm success".into())
        }
    }

    pub async fn close_and_wait(&self) -> Result<(), String> {
        // Dropping the pipe, rather than AsyncWrite::shutdown, guarantees EOF.
        self.input.lock().await.take();
        let mut child = self.child.lock().await;
        match timeout(Duration::from_secs(55), child.wait()).await {
            Ok(Ok(status)) if status.success() => Ok(()),
            Ok(_) => Err("Networking worker failed during cleanup".into()),
            Err(_) => {
                let _ = child.kill().await;
                Err("Networking worker shutdown timed out; cleanup unconfirmed".into())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture(std::path::PathBuf);
    impl Fixture {
        fn new(stop_success: bool) -> Self {
            let path = std::env::temp_dir().join(format!(
                "relayforge-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir(&path).unwrap();
            std::fs::create_dir(path.join("compat")).unwrap();
            let script = r#"
import json,sys
from pathlib import Path
def emit(value):
    print(json.dumps(value),flush=True)
emit({'ready':1})
for line in sys.stdin:
    request=json.loads(line)
    method=request['method']
    if method=='GetStatus':
        emit({'signal':'StatusChanged','data':{'active':False}})
        emit({'id':request['id'],'result':{'active':False}})
    elif method=='Start':
        emit({'id':request['id'],'error':'fixture rejection'})
    elif method=='GetClients':
        print('malformed output',flush=True)
        break
    else:
        emit({'id':request['id'],'result':STOP_SUCCESS})
Path(__file__).with_suffix('.closed').write_text('EOF')
"#
            .replace("STOP_SUCCESS", if stop_success { "True" } else { "False" });
            std::fs::write(path.join("compat/worker.py"), script).unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[tokio::test]
    async fn observations_are_applied_before_replies_and_shutdown_closes_stdin() {
        let root = Fixture::new(true);
        let (sender, mut events) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        let caller = worker.clone();
        let task = tokio::spawn(async move { caller.call(Method::GetStatus, Value::Null).await });
        let (event, ack) = timeout(Duration::from_secs(3), events.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(event["signal"], "StatusChanged");
        assert!(!task.is_finished());
        ack.send(()).unwrap();
        assert_eq!(task.await.unwrap().unwrap()["active"], false);
        timeout(Duration::from_secs(3), worker.shutdown())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            std::fs::read_to_string(root.0.join("compat/worker.closed")).unwrap(),
            "EOF"
        );
    }

    #[tokio::test]
    async fn transport_failure_fails_pending_calls_and_reports_loss() {
        let root = Fixture::new(true);
        let (sender, mut events) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        assert!(timeout(
            Duration::from_secs(3),
            worker.call(Method::GetClients, Value::Null)
        )
        .await
        .unwrap()
        .is_err());
        let (event, _) = events.recv().await.unwrap();
        assert_eq!(event["lost"], true);
        worker.close_and_wait().await.unwrap();
    }

    #[tokio::test]
    async fn rejected_operation_and_failed_cleanup_are_not_reported_as_success() {
        let root = Fixture::new(false);
        let (sender, _) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        for method in [
            Method::GetConfig,
            Method::SetConfig,
            Method::GetInterfaces,
            Method::GetCapabilities,
        ] {
            assert!(worker
                .call(method, Value::Null)
                .await
                .unwrap_err()
                .contains("delegation is forbidden"));
        }
        assert_eq!(
            worker.call(Method::Start, Value::Null).await.unwrap_err(),
            "Networking worker rejected the operation"
        );
        assert!(timeout(Duration::from_secs(3), worker.shutdown())
            .await
            .unwrap()
            .is_err());
    }

    #[tokio::test]
    async fn rejects_unbounded_and_truncated_worker_output() {
        let bytes = b"x".repeat(MAX_MESSAGE + 1);
        let mut huge = BufReader::new(&bytes[..]);
        assert!(read_frame(&mut huge).await.is_err());
        let mut partial = BufReader::new(&b"{}"[..]);
        assert!(read_frame(&mut partial).await.is_err());
        let mut valid = BufReader::new(&b"{\"id\":1,\"result\":true}\n"[..]);
        assert_eq!(read_frame(&mut valid).await.unwrap().unwrap()["id"], 1);
    }
}
