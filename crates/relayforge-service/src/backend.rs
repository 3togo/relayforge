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
#[derive(Default)]
struct Pending {
    requests: HashMap<u64, oneshot::Sender<Reply>>,
    disconnected: bool,
}
type SharedPending = Arc<Mutex<Pending>>;
pub type Event = (Value, oneshot::Sender<()>);

/// Registration belongs to the call future, including when its caller cancels it.
struct Request {
    pending: SharedPending,
    id: u64,
}
impl Request {
    fn register(
        pending: &SharedPending,
        id: u64,
        sender: oneshot::Sender<Reply>,
    ) -> Result<Self, String> {
        let mut state = pending.lock().unwrap();
        if state.disconnected {
            return Err("Networking worker disconnected".into());
        }
        if state.requests.len() >= 32 {
            return Err("Too many pending operations".into());
        }
        state.requests.insert(id, sender);
        Ok(Self {
            pending: pending.clone(),
            id,
        })
    }
}
impl Drop for Request {
    fn drop(&mut self) {
        self.pending.lock().unwrap().requests.remove(&self.id);
    }
}

/// A cancelled or failed write must close stdin: a partial frame cannot be reused.
struct Frame<'a> {
    input: tokio::sync::MutexGuard<'a, Option<ChildStdin>>,
    complete: bool,
}
impl Drop for Frame<'_> {
    fn drop(&mut self) {
        if !self.complete {
            self.input.take();
        }
    }
}

pub struct Worker {
    input: tokio::sync::Mutex<Option<ChildStdin>>,
    child: tokio::sync::Mutex<Child>,
    pending: SharedPending,
    next: AtomicU64,
    reader: tokio::task::AbortHandle,
}
impl Drop for Worker {
    fn drop(&mut self) {
        // The reader can be blocked waiting for an event acknowledgement even
        // after the child exits. Do not detach it or retain its pipes/state.
        self.reader.abort();
    }
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
        let input = child.stdin.take().ok_or("Missing worker input")?;
        let pending = Arc::new(Mutex::new(Pending::default()));
        let replies = pending.clone();
        let reader = tokio::spawn(async move {
            while let Ok(Some(value)) = read_frame(&mut output).await {
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    let sender = replies.lock().unwrap().requests.remove(&id);
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
            {
                let mut state = replies.lock().unwrap();
                state.disconnected = true;
                for (_, sender) in state.requests.drain() {
                    let _ = sender.send(Err("Networking worker disconnected".into()));
                }
            }
            let (ack, _) = oneshot::channel();
            let _ = events.send((json!({"lost":true}), ack)).await;
        });
        Ok(Arc::new(Self {
            input: tokio::sync::Mutex::new(Some(input)),
            child: tokio::sync::Mutex::new(child),
            pending,
            next: AtomicU64::new(1),
            reader: reader.abort_handle(),
        }))
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
        let _request = Request::register(&self.pending, id, sender)?;
        let mut bytes =
            serde_json::to_vec(&json!({"id":id,"method":method.name(),"params":params})).unwrap();
        bytes.push(b'\n');
        let result = async {
            {
                let mut frame = Frame {
                    input: self.input.lock().await,
                    complete: false,
                };
                frame
                    .input
                    .as_mut()
                    .ok_or("Worker input closed".to_owned())?
                    .write_all(&bytes)
                    .await
                    .map_err(|_| "Worker input closed".to_owned())?;
                frame.complete = true;
            }
            receiver.await.map_err(|_| "Worker reply lost".to_owned())?
        };
        let result = timeout(Duration::from_secs(90), result)
            .await
            .unwrap_or_else(|_| Err("Worker operation timed out; cleanup unconfirmed".into()));
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
        assert_eq!(
            timeout(
                Duration::from_secs(1),
                worker.call(Method::Stop, Value::Null)
            )
            .await
            .unwrap()
            .unwrap_err(),
            "Networking worker disconnected"
        );
        worker.close_and_wait().await.unwrap();
    }

    #[tokio::test]
    async fn cancelled_calls_release_capacity_and_ignore_late_replies() {
        let root = Fixture::new(true);
        let (sender, mut events) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        for _ in 0..40 {
            let caller = worker.clone();
            let task =
                tokio::spawn(async move { caller.call(Method::GetStatus, Value::Null).await });
            let (_, ack) = timeout(Duration::from_secs(3), events.recv())
                .await
                .unwrap()
                .unwrap();
            task.abort();
            assert!(task.await.unwrap_err().is_cancelled());
            assert!(worker.pending.lock().unwrap().requests.is_empty());
            ack.send(()).unwrap();
        }
        timeout(Duration::from_secs(3), worker.shutdown())
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn cancellation_waiting_for_the_writer_does_not_close_healthy_input() {
        let root = Fixture::new(true);
        let (sender, _) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        let input = worker.input.lock().await;
        let caller = worker.clone();
        let task = tokio::spawn(async move { caller.call(Method::Stop, Value::Null).await });
        timeout(Duration::from_secs(3), async {
            while worker.pending.lock().unwrap().requests.is_empty() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert!(worker.pending.lock().unwrap().requests.is_empty());
        assert!(input.is_some());
        drop(input);
        timeout(Duration::from_secs(3), worker.shutdown())
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn cancelled_partial_write_closes_stdin_and_allows_eof_cleanup() {
        use std::os::fd::AsRawFd;
        let root = Fixture::new(true);
        std::fs::write(
            root.0.join("compat/worker.py"),
            r#"
import sys,time
from pathlib import Path
root=Path(__file__).parent
print('{"ready":1}',flush=True)
while not (root/'read').exists():
    time.sleep(0.01)
data=sys.stdin.buffer.read()
(root/'received').write_bytes(data)
"#,
        )
        .unwrap();
        let (sender, _) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        {
            let input = worker.input.lock().await;
            // Ensure the frame cannot fit while the fixture deliberately does not read.
            assert!(
                unsafe {
                    libc::fcntl(
                        input.as_ref().unwrap().as_raw_fd(),
                        libc::F_SETPIPE_SZ,
                        4096,
                    )
                } > 0
            );
        }
        assert!(timeout(
            Duration::from_millis(100),
            worker.call(Method::Start, json!("x".repeat(32768)))
        )
        .await
        .is_err());
        assert!(worker.pending.lock().unwrap().requests.is_empty());
        assert!(worker.input.lock().await.is_none());
        assert!(timeout(
            Duration::from_secs(1),
            worker.call(Method::Stop, Value::Null)
        )
        .await
        .unwrap()
        .is_err());
        std::fs::write(root.0.join("compat/read"), "").unwrap();
        timeout(Duration::from_secs(3), worker.close_and_wait())
            .await
            .unwrap()
            .unwrap();
        let received = std::fs::read(root.0.join("compat/received")).unwrap();
        assert!(!received.is_empty());
        assert!(!received.ends_with(b"\n"));
        assert!(received.len() < 32768);
    }

    #[tokio::test]
    async fn dropping_worker_releases_reader_waiting_for_event_acknowledgement() {
        let root = Fixture::new(true);
        let (sender, mut events) = mpsc::channel(8);
        let worker = Worker::spawn(&root.0, sender).await.unwrap();
        let pending = Arc::downgrade(&worker.pending);
        let caller = worker.clone();
        let task = tokio::spawn(async move { caller.call(Method::GetStatus, Value::Null).await });
        let (_, ack) = timeout(Duration::from_secs(3), events.recv())
            .await
            .unwrap()
            .unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        drop(worker);
        timeout(Duration::from_secs(3), async {
            while pending.upgrade().is_some() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(ack.send(()).is_err());
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
