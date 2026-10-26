//! Supervised, unattended Linux X4 authority. No engine hooks or automatic world restart.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use crate::json::{quote, Json};
use crate::relay::Relay;
use crate::session::{LeaseSource, Level, Session};
use crate::wire::Slot;

const LINUX_X4_SHA256: &str = "113f73d74570084f6ce18a659b365009eccf579fb66dbabb5dad2b1718045f20";
const MAX_LOG: u64 = 32 * 1024 * 1024;
const MAX_READ: u64 = 1024 * 1024;

#[derive(Clone, Debug)]
pub struct Config {
    pub game: PathBuf,
    pub profile: PathBuf,
    pub package: PathBuf,
    pub session: PathBuf,
    pub work: PathBuf,
    pub display: String,
    pub startup_timeout: u64,
    pub shutdown_timeout: u64,
    pub heartbeat_timeout: u64,
    /// Optional engine agent (`libx4mp_agent.so`), preloaded only when configured.
    pub agent: Option<PathBuf>,
}

impl Config {
    pub fn load(path: &Path) -> Result<Self, String> {
        let path = fs::canonicalize(path).map_err(|e| format!("cannot resolve {}: {e}", path.display()))?;
        let text = fs::read_to_string(&path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        let root = Json::parse(&text)?;
        let base = path.parent().unwrap_or(Path::new("."));
        let required = |name: &str| -> Result<PathBuf, String> {
            let value = root.get(name).and_then(Json::as_str).ok_or_else(|| format!("missing string {name}"))?;
            if value.is_empty() {
                return Err(format!("empty path {name}"));
            }
            let path = PathBuf::from(value);
            Ok(if path.is_absolute() { path } else { base.join(path) })
        };
        let timeout = |name: &str, default: u64| -> Result<u64, String> {
            match root.get(name) {
                None => Ok(default),
                Some(value) => value
                    .as_f64()
                    .filter(|v| v.is_finite() && *v >= 1.0 && *v <= 3600.0 && v.fract() == 0.0)
                    .map(|v| v as u64)
                    .ok_or_else(|| format!("{name} must be an integer from 1 to 3600")),
            }
        };
        let display = root.get("display").and_then(Json::as_str).unwrap_or(":0").to_string();
        if display.is_empty() || !display.starts_with(':') || display.contains(['/', '\0']) {
            return Err("display must be a local X11 display such as :0".into());
        }
        let agent = match root.get("engine_agent") {
            None => None,
            Some(value) => {
                let text = value.as_str().filter(|t| !t.is_empty()).ok_or("engine_agent must be a non-empty path")?;
                let path = PathBuf::from(text);
                let path = if path.is_absolute() { path } else { base.join(path) };
                if !fs::metadata(&path).map(|m| m.is_file()).unwrap_or(false) {
                    return Err(format!("engine_agent is not a regular file: {}", path.display()));
                }
                Some(path)
            }
        };
        Ok(Config {
            agent,
            game: required("game")?,
            profile: required("profile")?,
            package: required("package")?,
            session: required("session")?,
            work: required("work")?,
            display,
            startup_timeout: timeout("startup_timeout_s", 240)?,
            shutdown_timeout: timeout("shutdown_timeout_s", 30)?,
            heartbeat_timeout: timeout("heartbeat_timeout_s", 15)?,
        })
    }

    fn control(&self) -> PathBuf {
        self.work.join("control.txt")
    }

    fn status(&self) -> PathBuf {
        self.work.join("status.json")
    }
}

fn regular_file(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|e| format!("cannot inspect {}: {e}", path.display()))?;
    if !meta.file_type().is_file() || meta.file_type().is_symlink() {
        return Err(format!("{} must be a regular, non-symlink file", path.display()));
    }
    Ok(())
}

fn regular_dir(path: &Path) -> Result<(), String> {
    let meta = fs::symlink_metadata(path).map_err(|e| format!("cannot inspect {}: {e}", path.display()))?;
    if !meta.file_type().is_dir() || meta.file_type().is_symlink() {
        return Err(format!("{} must be a regular, non-symlink directory", path.display()));
    }
    Ok(())
}

fn json_file(path: &Path) -> Result<Json, String> {
    regular_file(path)?;
    let text = fs::read_to_string(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    Json::parse(&text).map_err(|e| format!("{}: {e}", path.display()))
}

fn sha256(path: &Path) -> Result<String, String> {
    let output = Command::new("sha256sum").arg(path).output().map_err(|e| format!("cannot run sha256sum: {e}"))?;
    if !output.status.success() {
        return Err(format!("sha256sum failed for {}", path.display()));
    }
    let text = String::from_utf8(output.stdout).map_err(|e| e.to_string())?;
    let digest = text.split_whitespace().next().unwrap_or_default();
    if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(format!("invalid sha256sum output for {}", path.display()));
    }
    Ok(digest.to_string())
}

/// The Steam build of X4 refuses to start without a running, logged-in Steam client for
/// the same user: it shows a modal "X4 - Startup error" ("Steam client is required")
/// and never exits, so the supervisor would only notice at the startup timeout. The
/// client records its PID in `$HOME/.steam/steam.pid`; require that process to exist.
/// This is a fast, fail-closed precondition, not proof of login or ownership.
pub fn steam_client_running(home: &Path) -> Result<(), String> {
    let pid_file = home.join(".steam/steam.pid");
    let text = fs::read_to_string(&pid_file).map_err(|_| {
        format!(
            "Steam client is not running for this user ({} missing); the Steam build of X4 stops with \
             'Steam client is required'. Start and log in the Linux Steam client first.",
            pid_file.display()
        )
    })?;
    let pid = text.trim();
    if pid.is_empty() || !pid.bytes().all(|b| b.is_ascii_digit()) || pid.parse::<u32>().map_or(true, |v| v == 0) {
        return Err(format!("invalid Steam client PID file {}", pid_file.display()));
    }
    if !Path::new("/proc").join(pid).is_dir() {
        return Err(format!(
            "Steam client PID {pid} from {} is not running; start and log in the Linux Steam client first",
            pid_file.display()
        ));
    }
    Ok(())
}

fn shared_files(root: &Path) -> Result<Vec<PathBuf>, String> {
    let mut files = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        for entry in fs::read_dir(&folder).map_err(|e| format!("cannot list {}: {e}", folder.display()))? {
            let path = entry.map_err(|e| e.to_string())?.path();
            let relative = path.strip_prefix(root).map_err(|e| e.to_string())?;
            let name = relative.to_string_lossy().replace('\\', "/");
            if name.starts_with('.') || name.starts_with("native/") || name == "ui/config.lua" {
                continue;
            }
            let meta = fs::symlink_metadata(&path).map_err(|e| e.to_string())?;
            if meta.file_type().is_symlink() {
                return Err(format!("extension link is unsupported: {}", path.display()));
            }
            if meta.file_type().is_dir() {
                pending.push(path);
            } else if meta.file_type().is_file() {
                files.push(relative.to_path_buf());
            }
        }
    }
    files.sort();
    Ok(files)
}

fn compare_extensions(package: &Path, installed: &Path) -> Result<(), String> {
    let expected = shared_files(package)?;
    let actual = shared_files(installed)?;
    if expected != actual {
        return Err("installed multiplayer script inventory differs from the package".into());
    }
    for relative in expected {
        if fs::read(package.join(&relative)).map_err(|e| e.to_string())?
            != fs::read(installed.join(&relative)).map_err(|e| e.to_string())?
        {
            return Err(format!("installed multiplayer script differs: {}", relative.display()));
        }
    }
    regular_file(&installed.join("native/x4lan_net.so"))?;
    Ok(())
}

#[derive(Clone, Debug)]
pub struct Prepared {
    pub session: Json,
    pub level: Level,
    pub token: String,
    pub build: String,
    pub bubble: String,
    pub port: u16,
}

pub fn preflight(config: &Config) -> Result<Prepared, String> {
    regular_file(&config.game)?;
    regular_dir(&config.profile)?;
    if config.profile.file_name().and_then(|v| v.to_str()) != Some("45841") {
        return Err("dedicated server requires isolated profile 45841".into());
    }
    if sha256(&config.game)? != LINUX_X4_SHA256 {
        return Err("Linux X4 executable differs from pinned 9.00 build 23660954".into());
    }
    let version = fs::read_to_string(config.game.with_file_name("version.dat")).map_err(|e| e.to_string())?;
    if version.trim() != "900" {
        return Err("dedicated server requires X4 9.00".into());
    }
    let home = std::env::var_os("HOME").ok_or("HOME is not set; cannot locate the Steam client")?;
    steam_client_running(Path::new(&home))?;
    let profile_config = config.profile.join("config.xml");
    regular_file(&profile_config)?;
    let xml = fs::read_to_string(&profile_config).map_err(|e| e.to_string())?;
    for setting in ["autosave", "uisafemode"] {
        if !xml.contains(&format!("<{setting}>false</{setting}>")) {
            return Err(format!("profile 45841 must keep {setting}=false"));
        }
    }
    for setting in ["load", "loadgame", "module", "mpar"] {
        if xml.contains(&format!("<{setting}>")) || xml.contains(&format!("<{setting} ")) {
            return Err(format!("profile contains conflicting {setting} override"));
        }
    }
    let manifest = json_file(&config.package.join("world/bundle/manifest.json"))?;
    let prepared = json_file(&config.package.join("world/prepared.json"))?;
    let session = json_file(&config.session)?;
    let field = |root: &Json, name: &str| root.get(name).and_then(Json::as_str).map(str::to_string);
    let seed = field(&manifest, "seed_id").ok_or("world manifest has no seed_id")?;
    let digest = field(&manifest, "sha256").ok_or("world manifest has no SHA-256")?;
    if prepared.get("native_completed") != Some(&Json::Bool(true))
        || field(&prepared, "seed_id").as_deref() != Some(seed.as_str())
        || field(&prepared, "sha256").as_deref() != Some(digest.as_str())
        || field(&session, "bubble").as_deref() != Some(seed.as_str())
        || field(&manifest, "game_build") != field(&session, "build")
    {
        return Err("prepared world/session identity mismatch".into());
    }
    let format = field(&manifest, "format").unwrap_or_default();
    let save_name = match format.as_str() {
        "x4-xml-gzip" => "save_010.xml.gz",
        "x4-xml" => "save_010.xml",
        _ => return Err("unsupported prepared save format".into()),
    };
    let save = config.profile.join("save").join(save_name);
    regular_file(&save)?;
    if sha256(&save)? != digest {
        return Err("isolated save differs from the owned pristine snapshot".into());
    }
    let saves = config.profile.join("save");
    regular_dir(&saves)?;
    let mut inventory = fs::read_dir(&saves)
        .map_err(|e| e.to_string())?
        .map(|row| row.map(|e| e.file_name()).map_err(|e| e.to_string()))
        .collect::<Result<Vec<_>, _>>()?;
    inventory.retain(|name| name != "steam_autocloud.vdf");
    if inventory.len() != 1 || inventory[0] != save_name {
        return Err("unexpected saves in isolated profile 45841".into());
    }
    let game_root = config.game.parent().ok_or("game executable has no installation directory")?;
    compare_extensions(&config.package.join("extension/x4_lan_lab"), &game_root.join("extensions/x4_lan_lab"))?;
    let token = field(&session, "token").ok_or("session has no token")?;
    let build = field(&session, "build").ok_or("session has no build")?;
    let port = session.get("port").and_then(Json::as_f64).unwrap_or(45841.0);
    if port.fract() != 0.0 || !(1.0..=65535.0).contains(&port) {
        return Err("session port is invalid".into());
    }
    let installed_config = game_root.join("extensions/x4_lan_lab/ui/config.lua");
    regular_file(&installed_config)?;
    let lua = fs::read_to_string(&installed_config).map_err(|e| e.to_string())?;
    let compact: String = lua.chars().filter(|c| !c.is_whitespace()).collect();
    let mut required = vec![
        "enabled=true".to_string(),
        "bubble_actions=true".to_string(),
        "coop=true".to_string(),
        "world_sync=true".to_string(),
        "dedicated=true".to_string(),
        "slot=\"p1\"".to_string(),
        "test_profile=\"45841\"".to_string(),
        "seed_verified=true".to_string(),
        "host=\"127.0.0.1\"".to_string(),
        format!("port={}", port as u16),
        format!("build={}", quote(&build)),
        format!("bubble={}", quote(&seed)),
        format!("token={}", quote(&token)),
        format!("dedicated_control_path={}", quote(&config.control().to_string_lossy())),
    ];
    required.retain(|value| !compact.contains(value.as_str()));
    if !required.is_empty() {
        return Err("installed Lua config is not the expected dedicated authority configuration".into());
    }
    let number = config.display[1..].split('.').next().unwrap_or_default();
    if number.is_empty() || !number.bytes().all(|b| b.is_ascii_digit()) {
        return Err("invalid local X11 display".into());
    }
    if !Path::new("/tmp/.X11-unix").join(format!("X{number}")).exists() {
        return Err(format!("display {} is unavailable; WSLg/X11 must be running", config.display));
    }
    if session.get("dedicated") != Some(&Json::Bool(true)) {
        return Err("session does not enable dedicated authority plus two guest slots".into());
    }
    let level = if session.get("world_sync").is_some_and(Json::truthy) {
        Level::Universe
    } else if session.get("coop").is_some_and(Json::truthy) {
        Level::Coop
    } else {
        Level::Economy
    };
    Ok(Prepared { session, level, token, build, bubble: seed, port: port as u16 })
}

#[derive(Clone, Debug, PartialEq)]
pub struct Heartbeat {
    pub game_time: f64,
    pub real_time: f64,
    pub paused: bool,
    pub sector: String,
}

pub fn heartbeat(line: &str) -> Result<Option<Heartbeat>, String> {
    let Some(body) = line.split("[X4LAN] DEDICATED HEARTBEAT ").nth(1) else {
        return Ok(None);
    };
    if body.len() > 1024 {
        return Err("oversize heartbeat".into());
    }
    let mut game = None;
    let mut real = None;
    let mut paused = None;
    let mut sector = None;
    for item in body.split_whitespace() {
        let (key, value) = item.split_once('=').ok_or("malformed heartbeat field")?;
        match key {
            "game" => game = Some(value.parse::<f64>().map_err(|_| "invalid heartbeat game time")?),
            "real" => real = Some(value.parse::<f64>().map_err(|_| "invalid heartbeat real time")?),
            "paused" => {
                paused = Some(match value {
                    "0" => false,
                    "1" => true,
                    _ => return Err("invalid pause state".into()),
                })
            }
            "sector" => sector = Some(value.to_string()),
            "build" | "callbacks" => {}
            _ => return Err("unknown heartbeat field".into()),
        }
    }
    let (Some(game_time), Some(real_time), Some(paused), Some(sector)) = (game, real, paused, sector) else {
        return Err("incomplete heartbeat".into());
    };
    if !game_time.is_finite()
        || !real_time.is_finite()
        || game_time < 0.0
        || real_time < 0.0
        || sector.is_empty()
        || sector.len() > 96
        || !sector.bytes().all(|b| b.is_ascii_alphanumeric() || b"_-.".contains(&b))
    {
        return Err("invalid heartbeat values".into());
    }
    Ok(Some(Heartbeat { game_time, real_time, paused, sector }))
}

fn write_atomic(path: &Path, data: &[u8]) -> Result<(), String> {
    let parent = path.parent().ok_or("atomic output has no parent")?;
    regular_dir(parent)?;
    let temporary = path.with_extension(format!("tmp-{}", std::process::id()));
    {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&temporary).map_err(|e| e.to_string())?;
        file.write_all(data).map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
    }
    fs::rename(&temporary, path).map_err(|e| format!("cannot replace {}: {e}", path.display()))
}

pub fn command(config: &Config, action: &str) -> Result<(), String> {
    if !matches!(action, "shutdown" | "pause" | "resume") {
        return Err("unsupported dedicated command".into());
    }
    let session = json_file(&config.session)?;
    let bubble = session.get("bubble").and_then(Json::as_str).ok_or("session has no bubble")?;
    let path = config.control();
    let previous = fs::read_to_string(&path).unwrap_or_default();
    let generation = previous.split('|').next().and_then(|v| v.parse::<u64>().ok()).unwrap_or(0).saturating_add(1);
    if generation == u64::MAX {
        return Err("control generation exhausted".into());
    }
    let value = format!("{generation}|{bubble}|{action}\n");
    write_atomic(&path, value.as_bytes())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Starting,
    Ready,
    Degraded,
    Stopping,
    Stopped,
    Failed,
}

pub struct Supervisor {
    config: Config,
    prepared: Prepared,
    relay: Relay,
    child: Option<Child>,
    state: State,
    reason: String,
    started: Instant,
    log_offset: u64,
    pending: String,
    heartbeat: Option<Heartbeat>,
    heartbeat_at: Option<Instant>,
    progressed: bool,
    control_generation: u64,
    stopping_at: Option<Instant>,
}

impl Supervisor {
    pub fn new(config: Config) -> Result<Self, String> {
        let prepared = preflight(&config)?;
        fs::create_dir_all(&config.work).map_err(|e| format!("cannot create {}: {e}", config.work.display()))?;
        fs::create_dir_all(config.work.join("tmp"))
            .map_err(|e| format!("cannot create dedicated temp directory: {e}"))?;
        regular_dir(&config.work)?;
        let session = Session::new_dedicated(
            prepared.level,
            &prepared.token,
            &prepared.build,
            &prepared.bubble,
            LeaseSource::Os,
        )?;
        let bind = relay_bind_address(std::env::var("X4LAN_RELAY_BIND").ok().as_deref())?;
        let relay = Relay::bind(&bind, prepared.port, session).map_err(|e| format!("cannot bind relay: {e}"))?;
        let control_generation = fs::read_to_string(config.control())
            .unwrap_or_default()
            .split('|')
            .next()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(0);
        Ok(Supervisor {
            config,
            prepared,
            relay,
            child: None,
            state: State::Starting,
            reason: String::new(),
            started: Instant::now(),
            log_offset: 0,
            pending: String::new(),
            heartbeat: None,
            heartbeat_at: None,
            progressed: false,
            control_generation,
            stopping_at: None,
        })
    }

    fn status(&self) -> String {
        let peers = self.relay.session.core.peers.len();
        let authority = self.relay.session.peer(Slot::P1).is_some();
        let heartbeat = self
            .heartbeat
            .as_ref()
            .map(|h| {
                format!(
                    ",\"game_time\":{},\"real_time\":{},\"paused\":{},\"sector\":{}",
                    h.game_time,
                    h.real_time,
                    h.paused,
                    quote(&h.sector)
                )
            })
            .unwrap_or_default();
        format!(
            "{{\"protocol\":1,\"state\":{},\"reason\":{},\"pid\":{},\"relay_peers\":{peers},\"authority_connected\":{authority},\"uptime_s\":{}{heartbeat}}}\n",
            quote(&format!("{:?}", self.state).to_lowercase()), quote(&self.reason),
            self.child.as_ref().map_or(0, Child::id), self.started.elapsed().as_secs()
        )
    }

    fn publish(&self) -> Result<(), String> {
        write_atomic(&self.config.status(), self.status().as_bytes())
    }

    fn spawn(&mut self) -> Result<(), String> {
        let log = self.config.profile.join("x4lan-dedicated-native.log");
        if log.exists() {
            let archived = self.config.work.join(format!("previous-{}.log", std::process::id()));
            fs::copy(&log, archived).map_err(|e| format!("cannot preserve previous native log: {e}"))?;
        }
        regular_file(&log).or_else(|e| if log.exists() { Err(e) } else { Ok(()) })?;
        let stdout = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.config.work.join("game-output.log"))
            .map_err(|e| e.to_string())?;
        let stderr = stdout.try_clone().map_err(|e| e.to_string())?;
        let child = Command::new(&self.config.game)
            .current_dir(self.config.game.parent().unwrap())
            .args([
                "-personalfolderid",
                "45841",
                "-module",
                "x4lan_internal",
                "-skipintro",
                "-debug",
                "scripts",
                "-logfile",
            ])
            .arg(&log)
            .args(["-nocputhrottle", "-noworkshopsync"])
            .env("DISPLAY", &self.config.display)
            .env("LD_LIBRARY_PATH", self.config.game.parent().unwrap().join("lib"))
            .env("TMPDIR", self.config.work.join("tmp"))
            .envs(agent_environment(&self.config))
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr))
            .spawn()
            .map_err(|e| format!("cannot launch Linux X4: {e}"))?;
        self.child = Some(child);
        self.log_offset = 0;
        self.publish()
    }

    fn poll_log(&mut self) -> Result<(), String> {
        let path = self.config.profile.join("x4lan-dedicated-native.log");
        if !path.exists() {
            return Ok(());
        }
        regular_file(&path)?;
        let mut file = File::open(&path).map_err(|e| e.to_string())?;
        let length = file.metadata().map_err(|e| e.to_string())?.len();
        if length > MAX_LOG || length < self.log_offset {
            return Err("native log overflow or truncation".into());
        }
        if length == self.log_offset {
            return Ok(());
        }
        file.seek(SeekFrom::Start(self.log_offset)).map_err(|e| e.to_string())?;
        let mut bytes = Vec::new();
        file.take(MAX_READ).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
        self.log_offset += bytes.len() as u64;
        self.pending.push_str(std::str::from_utf8(&bytes).map_err(|_| "native log is not UTF-8")?);
        while let Some(index) = self.pending.find('\n') {
            let line = self.pending[..index].trim_end_matches('\r').to_string();
            self.pending.drain(..=index);
            if line.contains("[X4LAN] DEDICATED STOPPED") {
                return Err(line);
            }
            if let Some(value) = heartbeat(&line)? {
                self.progressed =
                    self.heartbeat.as_ref().is_some_and(|old| value.game_time > old.game_time) || self.progressed;
                self.heartbeat = Some(value);
                self.heartbeat_at = Some(Instant::now());
            }
        }
        if self.pending.len() > 4096 {
            return Err("unterminated native log record".into());
        }
        Ok(())
    }

    fn fail(&mut self, reason: impl Into<String>) {
        self.reason = reason.into();
        self.state = State::Failed;
    }

    fn poll_control(&mut self) {
        let Ok(text) = fs::read_to_string(self.config.control()) else { return };
        let mut fields = text.trim_end().split('|');
        let (Some(generation), Some(session), Some(action)) = (fields.next(), fields.next(), fields.next()) else {
            return;
        };
        let Ok(generation) = generation.parse::<u64>() else { return };
        if generation <= self.control_generation || session != self.prepared.bubble {
            return;
        }
        self.control_generation = generation;
        if action == "shutdown" && !matches!(self.state, State::Stopping | State::Stopped | State::Failed) {
            self.state = State::Stopping;
            self.stopping_at = Some(Instant::now());
        }
    }

    fn tick(&mut self) -> Result<(), String> {
        self.relay.step(self.started.elapsed().as_secs_f64());
        self.poll_control();
        self.poll_log()?;
        if self.child.as_mut().is_some_and(|child| matches!(child.try_wait(), Ok(Some(_)))) {
            let clean = matches!(self.state, State::Stopping);
            self.child = None;
            self.state = if clean { State::Stopped } else { State::Failed };
            if !clean && self.reason.is_empty() {
                self.reason = "X4 exited unexpectedly; automatic world restart is disabled".into();
            }
        }
        let heartbeat_age = self.heartbeat_at.map(|at| at.elapsed());
        match self.state {
            State::Starting => {
                if self.progressed && self.relay.session.peer(Slot::P1).is_some() {
                    self.state = State::Ready;
                } else if heartbeat_age.is_some_and(|age| age > Duration::from_secs(self.config.heartbeat_timeout))
                    || self.started.elapsed() > Duration::from_secs(self.config.startup_timeout)
                {
                    self.fail("native simulation/authority readiness timed out");
                }
            }
            State::Ready => {
                if heartbeat_age.is_none_or(|age| age > Duration::from_secs(self.config.heartbeat_timeout)) {
                    self.fail("native simulation heartbeat stopped");
                } else if self.relay.session.peer(Slot::P1).is_none() {
                    self.state = State::Degraded;
                    self.reason = "authority peer disconnected".into();
                }
            }
            State::Degraded => {
                if self.relay.session.peer(Slot::P1).is_some() {
                    self.state = State::Ready;
                    self.reason.clear();
                } else if heartbeat_age.is_none_or(|age| age > Duration::from_secs(self.config.heartbeat_timeout)) {
                    self.fail("authority and native simulation heartbeat stopped");
                }
            }
            State::Stopping => {
                if self.stopping_at.is_some_and(|at| at.elapsed() > Duration::from_secs(self.config.shutdown_timeout)) {
                    self.fail("graceful native shutdown timed out");
                }
            }
            State::Stopped | State::Failed => {}
        }
        self.publish()
    }

    pub fn run(mut self) -> Result<(), String> {
        self.spawn()?;
        let result = loop {
            if matches!(self.state, State::Stopped) {
                break Ok(());
            }
            if matches!(self.state, State::Failed) {
                let _ = command(&self.config, "shutdown");
                let deadline = Instant::now() + Duration::from_secs(self.config.shutdown_timeout);
                while self.child.is_some() && Instant::now() < deadline {
                    std::thread::sleep(Duration::from_millis(100));
                    self.relay.step(self.started.elapsed().as_secs_f64());
                    if self.child.as_mut().is_some_and(|child| matches!(child.try_wait(), Ok(Some(_)))) {
                        self.child = None;
                    }
                }
                if let Some(child) = self.child.as_mut() {
                    let _ = child.kill();
                    let _ = child.wait();
                    self.child = None;
                }
                self.state = State::Stopped;
                let _ = self.publish();
                break Err(self.reason.clone());
            }
            if let Err(error) = self.tick() {
                self.fail(error);
            }
            std::thread::sleep(Duration::from_millis(100));
        };
        result
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Relay listen address: loopback unless the operator opts in, e.g. `0.0.0.0` on a server
/// whose firewall admits the session port. Only literal IP addresses are accepted.
fn relay_bind_address(value: Option<&str>) -> Result<String, String> {
    match value.map(str::trim) {
        None | Some("") => Ok("127.0.0.1".into()),
        Some(address) => address
            .parse::<std::net::IpAddr>()
            .map(|ip| ip.to_string())
            .map_err(|_| format!("X4LAN_RELAY_BIND is not an IP address: {address}")),
    }
}

/// LD_PRELOAD environment for the opt-in engine agent; empty when none is configured.
fn agent_environment(config: &Config) -> Vec<(&'static str, std::ffi::OsString)> {
    match &config.agent {
        None => Vec::new(),
        Some(agent) => vec![
            ("LD_PRELOAD", agent.clone().into_os_string()),
            ("X4MP_AGENT", "1".into()),
            ("X4MP_AGENT_LOG", config.work.join("engine-agent.log").into_os_string()),
        ],
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn relay_bind_defaults_to_loopback_and_accepts_only_ip_addresses() {
        assert_eq!(super::relay_bind_address(None).unwrap(), "127.0.0.1");
        assert_eq!(super::relay_bind_address(Some(" ")).unwrap(), "127.0.0.1");
        assert_eq!(super::relay_bind_address(Some("0.0.0.0")).unwrap(), "0.0.0.0");
        assert_eq!(super::relay_bind_address(Some("::")).unwrap(), "::");
        assert!(super::relay_bind_address(Some("example.com")).is_err());
    }

    #[test]
    fn engine_agent_is_off_by_default_and_preloaded_only_when_configured() {
        let work = scratch();
        let agent = work.join("libx4mp_agent.so");
        fs::write(&agent, b"").unwrap();
        let base = format!(
            "{{\"game\":\"g\",\"profile\":\"p\",\"package\":\"k\",\"session\":\"s\",\"work\":\"{}\"",
            work.display()
        );
        let path = work.join("config.json");
        fs::write(&path, format!("{base}}}")).unwrap();
        let config = Config::load(&path).unwrap();
        assert!(config.agent.is_none() && agent_environment(&config).is_empty());
        fs::write(&path, format!("{base},\"engine_agent\":\"libx4mp_agent.so\"}}")).unwrap();
        let config = Config::load(&path).unwrap();
        let env = agent_environment(&config);
        assert_eq!(env[0], ("LD_PRELOAD", fs::canonicalize(&work).unwrap().join("libx4mp_agent.so").into_os_string()));
        assert_eq!(env[1], ("X4MP_AGENT", "1".into()));
        assert_eq!(env[2].1, config.work.join("engine-agent.log").into_os_string());
        fs::write(&path, format!("{base},\"engine_agent\":\"missing.so\"}}")).unwrap();
        assert!(Config::load(&path).unwrap_err().contains("not a regular file"));
        fs::remove_dir_all(work).unwrap();
    }

    use super::*;

    #[test]
    fn parses_native_heartbeat_and_rejects_bad_records() {
        let value = heartbeat("[=ERROR=] [X4LAN] DEDICATED HEARTBEAT build=world1-test game=12.5 real=20.0 paused=0 sector=cluster_14_sector001_macro callbacks=60").unwrap().unwrap();
        assert_eq!(value.game_time, 12.5);
        assert_eq!(value.sector, "cluster_14_sector001_macro");
        assert!(heartbeat("[X4LAN] DEDICATED HEARTBEAT game=nan real=1 paused=0 sector=a_macro").is_err());
        assert!(heartbeat("[X4LAN] DEDICATED HEARTBEAT game=1 real=1 paused=yes sector=a_macro").is_err());
        assert_eq!(heartbeat("unrelated").unwrap(), None);
    }

    #[test]
    fn steam_client_must_be_running_before_launch() {
        let home = scratch();
        assert!(steam_client_running(&home).unwrap_err().contains("Steam client is not running"));
        fs::create_dir(home.join(".steam")).unwrap();
        for bad in ["", "abc", "0", "-1"] {
            fs::write(home.join(".steam/steam.pid"), bad).unwrap();
            assert!(steam_client_running(&home).unwrap_err().contains("invalid Steam client PID"));
        }
        fs::write(home.join(".steam/steam.pid"), "4294967295").unwrap();
        assert!(steam_client_running(&home).is_err());
        fs::write(home.join(".steam/steam.pid"), format!("{}\n", std::process::id())).unwrap();
        steam_client_running(&home).unwrap();
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn control_rejects_unknown_actions() {
        let config = Config {
            game: "unused".into(),
            profile: "unused".into(),
            package: "unused".into(),
            session: "unused".into(),
            work: "unused".into(),
            display: ":0".into(),
            startup_timeout: 1,
            shutdown_timeout: 1,
            heartbeat_timeout: 1,
            agent: None,
        };
        assert!(command(&config, "restart").is_err());
    }

    fn scratch() -> PathBuf {
        let unique = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let path = std::env::temp_dir().join(format!("x4lan-dedicated-{}-{unique}", std::process::id()));
        fs::create_dir(&path).unwrap();
        path
    }

    #[test]
    fn commands_are_atomic_monotonic_and_do_not_require_a_healthy_game() {
        let work = scratch();
        let session = work.join("session.json");
        fs::write(&session, format!("{{\"bubble\":\"{}\"}}", "a".repeat(32))).unwrap();
        let config = Config {
            game: "missing".into(),
            profile: "missing".into(),
            package: "missing".into(),
            session,
            work: work.clone(),
            display: ":0".into(),
            startup_timeout: 1,
            shutdown_timeout: 1,
            heartbeat_timeout: 1,
            agent: None,
        };
        command(&config, "pause").unwrap();
        command(&config, "resume").unwrap();
        let text = fs::read_to_string(config.control()).unwrap();
        assert_eq!(text, format!("2|{}|resume\n", "a".repeat(32)));
        fs::remove_dir_all(work).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn requested_shutdown_is_a_clean_stop_not_a_crash() {
        let work = scratch();
        let session_path = work.join("session.json");
        fs::write(&session_path, format!("{{\"bubble\":\"{}\"}}", "b".repeat(32))).unwrap();
        let config = Config {
            game: "/bin/true".into(),
            profile: work.join("missing-profile"),
            package: "missing".into(),
            session: session_path,
            work: work.clone(),
            display: ":0".into(),
            startup_timeout: 3600,
            shutdown_timeout: 1,
            heartbeat_timeout: 3600,
            agent: None,
        };
        let prepared = Prepared {
            session: Json::Null,
            level: Level::Universe,
            token: "a".repeat(32),
            build: "world1-test".into(),
            bubble: "b".repeat(32),
            port: 0,
        };
        let session =
            Session::new(Level::Universe, &"a".repeat(32), "world1-test", &"b".repeat(32), LeaseSource::Os).unwrap();
        let relay = Relay::bind("127.0.0.1", 0, session).unwrap();
        let mut supervisor = Supervisor {
            config,
            prepared,
            relay,
            child: None,
            state: State::Ready,
            reason: String::new(),
            started: Instant::now(),
            log_offset: 0,
            pending: String::new(),
            heartbeat: None,
            heartbeat_at: None,
            progressed: false,
            control_generation: 0,
            stopping_at: None,
        };
        supervisor.child = Some(Command::new("/bin/true").spawn().unwrap());
        command(&supervisor.config, "shutdown").unwrap();
        std::thread::sleep(Duration::from_millis(20));
        supervisor.tick().unwrap();
        assert_eq!(supervisor.state, State::Stopped);
        assert!(supervisor.child.is_none());
        assert!(fs::read_to_string(work.join("status.json")).unwrap().contains("\"state\":\"stopped\""));
        fs::remove_dir_all(work).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn child_crash_fails_without_spawning_a_replacement_world() {
        let work = scratch();
        let config = Config {
            game: "/bin/true".into(),
            profile: work.join("missing-profile"),
            package: "missing".into(),
            session: "missing".into(),
            work: work.clone(),
            display: ":0".into(),
            startup_timeout: 3600,
            shutdown_timeout: 1,
            heartbeat_timeout: 3600,
            agent: None,
        };
        let session =
            Session::new(Level::Universe, &"a".repeat(32), "world1-test", &"b".repeat(32), LeaseSource::Os).unwrap();
        let relay = Relay::bind("127.0.0.1", 0, session).unwrap();
        let prepared = Prepared {
            session: Json::Null,
            level: Level::Universe,
            token: "a".repeat(32),
            build: "world1-test".into(),
            bubble: "b".repeat(32),
            port: 0,
        };
        let mut supervisor = Supervisor {
            config,
            prepared,
            relay,
            child: None,
            state: State::Starting,
            reason: String::new(),
            started: Instant::now(),
            log_offset: 0,
            pending: String::new(),
            heartbeat: None,
            heartbeat_at: None,
            progressed: false,
            control_generation: 0,
            stopping_at: None,
        };
        supervisor.child = Some(Command::new("/bin/true").spawn().unwrap());
        std::thread::sleep(Duration::from_millis(20));
        supervisor.tick().unwrap();
        assert_eq!(supervisor.state, State::Failed);
        assert!(supervisor.child.is_none());
        assert!(supervisor.reason.contains("automatic world restart is disabled"));
        assert!(fs::read_to_string(work.join("status.json")).unwrap().contains("\"state\":\"failed\""));
        fs::remove_dir_all(work).unwrap();
    }
}
