//! logind sleep coordination. Intent is memory-only and never authorizes a new upstream.
use crate::{
    networkmanager::{self, Upstream},
    Service,
};
use futures_util::StreamExt;
use relayforge_core::{config::Config, Method, BUS_NAME, OBJECT_PATH};
use serde_json::json;
use std::time::{Duration, Instant};
use tokio::time::timeout;
use zbus::{zvariant::OwnedFd, Connection, Proxy};

const LOGIN: &str = "org.freedesktop.login1";
const PATH: &str = "/org/freedesktop/login1";
const MANAGER: &str = "org.freedesktop.login1.Manager";
const SETTLE: Duration = Duration::from_secs(4);

#[derive(Default)]
pub(crate) struct State {
    pub available: bool,
    pub sleeping: bool,
    pub revision: u64,
    intent: Option<(Config, Upstream)>,
    waiting: bool,
    cleanup: bool,
    announce: bool,
    cleanup_after: Option<Instant>,
    candidate: Option<(Upstream, Instant)>,
    after: Option<Instant>,
    attempts: u32,
}
impl State {
    pub fn cancel(&mut self) {
        self.revision = self.revision.wrapping_add(1);
        self.intent = None;
        self.waiting = false;
        self.candidate = None;
        self.attempts = 0;
    }
    pub fn blocked(&self) -> bool {
        self.sleeping || self.cleanup
    }
    pub fn owns_status(&self) -> bool {
        self.blocked() || self.waiting
    }
    pub fn arm(&mut self, config: Config, original: Upstream, revision: u64) {
        if self.available && !self.blocked() && self.revision == revision {
            self.intent = Some((config, original));
            self.waiting = false;
        }
    }
    fn sleep(&mut self, sleeping: bool) {
        if self.sleeping == sleeping {
            return;
        }
        self.revision = self.revision.wrapping_add(1);
        self.sleeping = sleeping;
        self.announce = true;
        self.candidate = None;
        self.after = Some(Instant::now() + SETTLE);
        if sleeping {
            self.cleanup = true;
            self.waiting = self.intent.is_some();
        }
    }
    fn stable(&mut self, observed: Upstream, now: Instant) -> Option<Upstream> {
        if self.blocked() || !self.waiting || self.after.is_some_and(|after| now < after) {
            return None;
        }
        let (_, original) = self.intent.as_ref()?;
        if !original.same_connection(&observed) {
            self.candidate = None;
            return None;
        }
        match &self.candidate {
            Some((candidate, since)) if *candidate == observed => {
                (now.duration_since(*since) >= SETTLE).then_some(observed)
            }
            _ => {
                self.candidate = Some((observed, now));
                None
            }
        }
    }
}

impl Service {
    async fn power_status(
        &self,
        connection: &Connection,
        epoch: u64,
        clean: bool,
        error: Option<&str>,
    ) {
        {
            let power = self.power.lock().unwrap();
            let mut core = self.core.lock().unwrap();
            if !core.is_current(epoch) {
                return;
            }
            core.status.desired_active = power.intent.is_some();
            if clean {
                core.status.active = false;
                core.status.client_count = 0;
            }
            core.status.state = if !clean {
                "failed"
            } else if power.sleeping {
                "suspended"
            } else if power.waiting {
                "waiting"
            } else {
                "idle"
            }
            .into();
            core.status.error = error.map(str::to_owned);
        }
        if clean {
            *self.clients.lock().unwrap() = json!([]);
            let _ = connection
                .emit_signal(
                    None::<&str>,
                    OBJECT_PATH,
                    BUS_NAME,
                    "ClientsChanged",
                    &("[]",),
                )
                .await;
        }
        self.emit_status(connection).await;
    }

    pub(crate) async fn power_tick(&self, connection: &Connection) {
        let Ok(_operation) = self.operations.try_lock() else {
            return;
        };
        let _config = self.config_users.read().await;
        let Some(native) = &self.native else {
            return;
        };
        let cleanup = self.power.lock().unwrap().cleanup;
        if cleanup {
            if self
                .power
                .lock()
                .unwrap()
                .cleanup_after
                .is_some_and(|after| Instant::now() < after)
            {
                return;
            }
            let epoch = match self.core.lock().unwrap().begin(Method::Stop) {
                Ok(epoch) => epoch,
                Err(_) => return,
            };
            let previous = self.core.lock().unwrap().status.clone();
            let clean = native.stop().await;
            self.core
                .lock()
                .unwrap()
                .complete(Method::Stop, epoch, clean);
            if clean {
                {
                    let mut core = self.core.lock().unwrap();
                    if core.is_current(epoch) {
                        core.status = relayforge_core::Status {
                            active: false,
                            client_count: 0,
                            error: None,
                            ..previous
                        };
                    }
                }
                let mut power = self.power.lock().unwrap();
                power.cleanup = false;
                power.announce = false;
                power.cleanup_after = None;
            }
            if !clean {
                self.power.lock().unwrap().cleanup_after =
                    Some(Instant::now() + Duration::from_secs(2));
            }
            self.power_status(
                connection,
                epoch,
                clean,
                if clean {
                    None
                } else {
                    Some("Sleep cleanup unconfirmed; resume is blocked")
                },
            )
            .await;
            return;
        }
        let announce = {
            let mut power = self.power.lock().unwrap();
            std::mem::take(&mut power.announce)
        };
        if announce {
            let (epoch, inactive) = {
                let core = self.core.lock().unwrap();
                (core.epoch(), !core.status.active)
            };
            if inactive {
                self.power_status(connection, epoch, true, None).await;
            }
        }
        let (config, revision) = {
            let power = self.power.lock().unwrap();
            if !power.available
                || power.sleeping
                || !power.waiting
                || power.after.is_some_and(|after| Instant::now() < after)
            {
                return;
            }
            let Some((config, _)) = &power.intent else {
                return;
            };
            (config.clone(), power.revision)
        };
        if self.load_config().await.ok().as_ref() != Some(&config) {
            self.power.lock().unwrap().cancel();
            let epoch = self.core.lock().unwrap().epoch();
            self.power_status(
                connection,
                epoch,
                true,
                Some("Settings changed; explicit Start is required"),
            )
            .await;
            return;
        }
        let mut cancelled = self.cancellation.subscribe();
        let observed = tokio::select! {
            result = timeout(Duration::from_secs(8), networkmanager::resume_probe(self.fixture, &config)) => result.ok().and_then(Result::ok),
            _ = cancelled.changed() => return,
        };
        let expected = {
            let mut power = self.power.lock().unwrap();
            if power.revision != revision {
                return;
            }
            match observed {
                Some(observed) => power.stable(observed, Instant::now()),
                None => {
                    power.candidate = None;
                    None
                }
            }
        };
        let Some(expected) = expected else {
            return;
        };
        let epoch = {
            let power = self.power.lock().unwrap();
            if power.revision != revision || power.blocked() {
                return;
            }
            match self.core.lock().unwrap().begin(Method::Start) {
                Ok(epoch) => epoch,
                Err(_) => return,
            }
        };
        self.emit_status(connection).await;
        let result = native.start(&config, cancelled, Some(&expected)).await;
        let success = result["success"] == true;
        {
            let mut power = self.power.lock().unwrap();
            let mut core = self.core.lock().unwrap();
            if power.revision != revision || !core.is_current(epoch) {
                return;
            }
            if let Some(status) = result.get("status") {
                let _ = core.observe(status.clone());
            }
            core.complete(Method::Start, epoch, success);
            if success {
                power.waiting = false;
                power.candidate = None;
                power.attempts = 0;
            } else {
                power.attempts = power.attempts.saturating_add(1);
                power.after =
                    Some(Instant::now() + Duration::from_secs(2u64.pow(power.attempts.min(6))));
                power.candidate = None;
                // Retry only after owned cleanup is positively confirmed.
                power.cleanup = true;
            }
        }
        if success {
            self.emit_status(connection).await;
        } else {
            self.power_status(
                connection,
                epoch,
                false,
                Some("Resume failed; verifying cleanup before retry"),
            )
            .await;
        }
    }
}

/// A unique-owner proxy excludes forged PrepareForSleep signals. The delay FD is
/// retained until owned cleanup succeeds (or logind's configured deadline expires).
pub(crate) async fn run(connection: Connection) {
    let interface = match connection
        .object_server()
        .interface::<_, Service>(OBJECT_PATH)
        .await
    {
        Ok(interface) => interface,
        Err(_) => return,
    };
    let service = interface.get().await;
    loop {
        let _ = monitor(&connection, &service).await;
        {
            let mut power = service.power.lock().unwrap();
            let monitored = power.available;
            power.available = false;
            power.cancel();
            // Losing logind invalidates the old intent and cancels in-flight resume.
            if monitored {
                power.cleanup = true;
                service
                    .cancellation
                    .send_modify(|revision| *revision = revision.wrapping_add(1));
                eprintln!("Sleep monitoring lost; automatic resume cancelled");
            }
            power.sleeping = false;
        }
        for _ in 0..10 {
            service.power_tick(&connection).await;
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }
}

async fn monitor(connection: &Connection, service: &Service) -> Result<(), ()> {
    let dbus = zbus::fdo::DBusProxy::new(connection)
        .await
        .map_err(|_| ())?;
    let owner = timeout(
        Duration::from_secs(3),
        dbus.get_name_owner(LOGIN.try_into().unwrap()),
    )
    .await
    .map_err(|_| ())?
    .map_err(|_| ())?;
    let proxy = Proxy::new(connection, owner.as_str(), PATH, MANAGER)
        .await
        .map_err(|_| ())?;
    let mut signals = proxy
        .receive_signal("PrepareForSleep")
        .await
        .map_err(|_| ())?;
    let mut inhibitor: Option<OwnedFd> = Some(
        timeout(
            Duration::from_secs(3),
            proxy.call(
                "Inhibit",
                &(
                    "sleep",
                    "RelayForge",
                    "Clean up owned hotspot before sleep",
                    "delay",
                ),
            ),
        )
        .await
        .map_err(|_| ())?
        .map_err(|_| ())?,
    );
    let properties = Proxy::new(
        connection,
        owner.as_str(),
        PATH,
        "org.freedesktop.DBus.Properties",
    )
    .await
    .map_err(|_| ())?;
    let value: zbus::zvariant::OwnedValue = timeout(
        Duration::from_secs(3),
        properties.call("Get", &(MANAGER, "PreparingForSleep")),
    )
    .await
    .map_err(|_| ())?
    .map_err(|_| ())?;
    let sleeping = bool::try_from(value).map_err(|_| ())?;
    {
        let mut power = service.power.lock().unwrap();
        power.available = true;
        power.sleep(sleeping);
        if sleeping {
            service
                .cancellation
                .send_modify(|revision| *revision = revision.wrapping_add(1));
        }
    }
    // Signal receipt must remain responsive while cleanup/startup awaits I/O.
    let (events, mut receiver) = tokio::sync::mpsc::channel::<bool>(16);
    let reader = async {
        let mut owner_timer = tokio::time::interval(Duration::from_millis(500));
        loop {
            tokio::select! {
                message = signals.next() => {
                    let message = message.ok_or(())?;
                    let (sleeping,): (bool,) = message.body().deserialize().map_err(|_| ())?;
                    {
                        let mut power = service.power.lock().unwrap();
                        power.sleep(sleeping);
                        if sleeping { service.cancellation.send_modify(|revision| *revision = revision.wrapping_add(1)); }
                    }
                    events.try_send(sleeping).map_err(|_| ())?;
                }
                _ = owner_timer.tick() => {
                    let current = timeout(Duration::from_secs(3), dbus.get_name_owner(LOGIN.try_into().unwrap())).await.map_err(|_| ())?.map_err(|_| ())?;
                    if current != owner { return Err::<(), ()>(()); }
                }
            }
        }
    };
    let worker = async {
        let mut timer = tokio::time::interval(Duration::from_millis(500));
        loop {
            tokio::select! {
                event = receiver.recv() => {
                    let sleeping = event.ok_or(())?;
                    if !sleeping {
                        inhibitor = Some(timeout(Duration::from_secs(3), proxy.call("Inhibit", &("sleep", "RelayForge", "Clean up owned hotspot before sleep", "delay"))).await.map_err(|_| ())?.map_err(|_| ())?);
                    }
                }
                _ = timer.tick() => {}
            }
            service.power_tick(connection).await;
            let power = service.power.lock().unwrap();
            if power.sleeping && !power.cleanup {
                inhibitor.take();
            }
        }
    };
    tokio::select! { result = reader => result, result = worker => result }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sleep_gate_and_stop_revision_are_independent_of_backend_results() {
        let mut state = State {
            available: true,
            ..State::default()
        };
        state.sleep(true);
        assert!(state.blocked());
        let revision = state.revision;
        state.cancel();
        assert_ne!(state.revision, revision);
        assert!(state.sleeping);
        state.sleep(false);
        assert!(state.cleanup);
        assert!(!state.waiting);
    }
    #[test]
    fn resume_requires_settled_connection_and_rejects_stale_authorization() {
        let now = Instant::now();
        let original = Upstream::test_connection(2412);
        let mut state = State {
            available: true,
            ..State::default()
        };
        state.arm(Config::new(), original.clone(), state.revision);
        state.sleep(true);
        state.sleep(false);
        state.cleanup = false;
        state.after = Some(now + SETTLE);
        assert!(state.stable(original.clone(), now).is_none());
        assert!(state.stable(original.clone(), now + SETTLE).is_none());
        let roamed = Upstream::test_connection(2437);
        assert!(state.stable(roamed.clone(), now + SETTLE * 2).is_none());
        assert!(state.stable(roamed.clone(), now + SETTLE * 3).is_some());
        state.cleanup = true;
        assert!(state.stable(roamed.clone(), now + SETTLE * 4).is_none());
        state.cleanup = false;
        let revision = state.revision;
        state.cancel();
        state.arm(Config::new(), original, revision);
        assert!(state.intent.is_none());
        assert!(state.stable(roamed, now + SETTLE * 5).is_none());
    }
}
