//! Opt-in local role broker: controller credentials never enter role commands.
//!
//! The broker is provisioned by the operator and authenticates the controller's
//! UID on a UNIX socket. It executes commands in distinct nonroot chroots and
//! confirms cleanup before returning. No in-process or unconfined fallback exists.
use std::collections::BTreeMap;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::MetadataExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde::Deserialize;

/// Explicitly supplied, non-secret, root-owned broker configuration.
pub const CONFIG_ENV: &str = "GEWU_ROLE_BROKER_CONFIG";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    controller_uid: u32,
    socket: PathBuf,
    roles: BTreeMap<String, Role>,
    #[serde(default)]
    _lock_dir: Option<PathBuf>,
    #[serde(default)]
    _forgejo_url: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Role {
    root: PathBuf,
    uid: u32,
    gid: u32,
}

/// Bounded response, containing only command output and cleanup state.
#[derive(Deserialize)]
pub struct Output {
    pub returncode: i32,
    pub stdout: String,
    pub stderr: String,
    pub termination: Option<String>,
    pub cleanup_confirmed: bool,
}

fn config() -> Result<Config, String> {
    let path = std::env::var_os(CONFIG_ENV).ok_or("role broker is not configured")?;
    let metadata = fs::symlink_metadata(&path).map_err(|_| "broker config is unavailable")?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
        return Err("broker config must be a root-owned, non-writable regular file".into());
    }
    let config: Config =
        serde_json::from_slice(&fs::read(path).map_err(|_| "broker config cannot be read")?)
            .map_err(|_| "invalid broker config")?;
    if rustix::process::geteuid().as_raw() != config.controller_uid {
        return Err("role broker requires its designated nonroot controller UID".into());
    }
    if config.roles.is_empty()
        || config
            .roles
            .values()
            .any(|role| role.uid < 20000 || role.gid < 20000)
    {
        return Err("invalid role identities".into());
    }
    Ok(config)
}

fn role_for<'a>(config: &'a Config, cwd: &Path) -> Result<(&'a str, PathBuf), String> {
    let resolved = cwd
        .canonicalize()
        .map_err(|_| "role working directory is unavailable")?;
    // A path that was presented under one registered workspace must not resolve
    // into another role's workspace. This preserves the original symlink escape
    // defense; the canonical comparison below is only for an explicitly
    // unregistered local-runtime alias.
    for role in config.roles.values() {
        let raw_workspace = role.root.join("workspace");
        if cwd.starts_with(&raw_workspace) {
            let raw = raw_workspace
                .canonicalize()
                .map_err(|_| "role workspace is unavailable")?;
            if !resolved.starts_with(&raw) {
                return Err("working-directory symlink escaped its original role".into());
            }
        }
    }
    for (name, role) in &config.roles {
        let workspace = role.root.join("workspace");
        // The local-runtime launcher may expose the registered chroot workspace
        // through a symlink. Compare canonical paths so the broker preserves the
        // registered role identity while rejecting links that escape it.
        let Ok(resolved_workspace) = workspace.canonicalize() else { continue; };
        if resolved.starts_with(&resolved_workspace) {
            let relative = resolved
                .strip_prefix(&resolved_workspace)
                .map_err(|_| "working-directory symlink escaped its original role")?;
            return Ok((name, Path::new("/workspace").join(relative)));
        }
    }
    Err("working directory is outside every registered role workspace".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn operator_lock_directory_is_accepted_by_the_controller() {
        let config: Config = serde_json::from_str(
            r#"{"controller_uid":65534,"socket":"@gewu-test","roles":{},"_lock_dir":"/tmp/gewu-locks"}"#,
        )
        .expect("operator lock directory is part of the broker contract");
        assert_eq!(config._lock_dir, Some(PathBuf::from("/tmp/gewu-locks")));
    }

    #[test]
    fn forgejo_url_in_operator_descriptor_is_accepted() {
        let config: Config = serde_json::from_str(
            r#"{"controller_uid":65534,"socket":"@gewu-test","roles":{},"_forgejo_url":"http://127.0.0.1:3000"}"#,
        )
        .expect("runtime broker descriptor may carry its Forgejo endpoint");
        assert_eq!(config._forgejo_url.as_deref(), Some("http://127.0.0.1:3000"));
    }

    #[test]
    fn directory_symlink_cannot_switch_role_identity() {
        let root = std::env::temp_dir().join(format!(
            "role-route-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        for role in ["author", "evaluator"] {
            fs::create_dir_all(root.join(role).join("workspace/repo")).unwrap();
        }
        let config = Config {
            controller_uid: 65534,
            socket: root.join("socket"),
            _lock_dir: None,
            _forgejo_url: None,
            roles: ["author", "evaluator"]
                .into_iter()
                .enumerate()
                .map(|(index, role)| {
                    (
                        role.to_owned(),
                        Role {
                            root: root.join(role),
                            uid: 21001 + index as u32,
                            gid: 21001 + index as u32,
                        },
                    )
                })
                .collect(),
        };
        let allowed = root.join("author/workspace/repo");
        assert_eq!(
            role_for(&config, &allowed).unwrap(),
            ("author", PathBuf::from("/workspace/repo"))
        );
        let escape = root.join("author/workspace/cross-role");
        std::os::unix::fs::symlink(root.join("evaluator/workspace/repo"), &escape).unwrap();
        assert!(role_for(&config, &escape).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[ignore = "requires a live operator broker and runtime workspace alias"]
    fn live_runtime_workspace_alias_is_available_to_the_controller() {
        let config_path = std::env::var_os(CONFIG_ENV).expect("broker config path");
        let workspace = std::env::var_os("GEWU_TEST_ROLE_WORKSPACE")
            .expect("runtime role workspace alias");
        let config: Config = serde_json::from_slice(&fs::read(config_path).unwrap()).unwrap();
        assert_eq!(rustix::process::geteuid().as_raw(), config.controller_uid);
        assert!(available(&[PathBuf::from(workspace)]));
    }
}

/// Check control-plane readiness without contending with any role command.
pub fn service_available() -> bool {
    config().and_then(|config| health(&config, None)).is_ok()
}

fn health(config: &Config, cwd: Option<&Path>) -> Result<(), String> {
    let (request, expected_role) = match cwd {
        Some(cwd) => {
            let (role, inside) = role_for(config, cwd)?;
            (serde_json::json!({"op":"health", "role":role, "cwd":inside}), Some(role))
        }
        None => (serde_json::json!({"op":"health"}), None),
    };
    let mut stream = connect(config)?;
    stream.set_read_timeout(Some(Duration::from_secs(30)))
        .map_err(|_| "broker health timeout cannot be set")?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "broker health timeout cannot be set")?;
    writeln!(stream, "{request}").map_err(|_| "broker health request failed")?;
    let mut response = Vec::new();
    use std::io::Read;
    BufReader::new(stream).take(16384).read_until(b'\n', &mut response)
        .map_err(|_| "broker health response unavailable")?;
    let response: serde_json::Value = serde_json::from_slice(&response)
        .map_err(|_| "invalid broker health response")?;
    if response["protocol"] != "role-health-v1" || response["ready"] != true
        || response["role"].as_str() != expected_role
    {
        return Err("broker health request refused".into());
    }
    Ok(())
}

/// Validate each requested role without starting or cleaning any role process.
pub fn available(workspaces: &[PathBuf]) -> bool {
    let Ok(config) = config() else { return false; };
    !workspaces.is_empty() && workspaces.iter().all(|cwd| health(&config, Some(cwd)).is_ok())
}

/// A run may only use the root registered to its own role name.
pub fn validate_role(cwd: &Path, agent: &str) -> Result<(), String> {
    let config = config()?;
    let (role, _) = role_for(&config, cwd)?;
    if role != agent {
        return Err("run identity does not match its registered role workspace".into());
    }
    Ok(())
}

/// Run a command through the operator's broker. Never execute on this host.
pub fn execute(cwd: &Path, argv: &[&str], timeout: u64) -> Result<Output, String> {
    let config = config()?;
    let (role, inside) = role_for(&config, cwd)?;
    let stream = connect(&config)?;
    stream
        .set_read_timeout(Some(Duration::from_secs(timeout.min(14400) + 30)))
        .map_err(|_| "broker timeout cannot be set")?;
    stream
        .set_write_timeout(Some(Duration::from_secs(120)))
        .map_err(|_| "broker timeout cannot be set")?;
    let request = serde_json::json!({"role": role, "cwd": inside, "argv": argv, "timeout": timeout.clamp(1,14400)});
    send_command(stream, request)
}

fn connect(config: &Config) -> Result<UnixStream, String> {
    let socket_name = config.socket.to_string_lossy();
    let stream = if let Some(name) = socket_name.strip_prefix('@') {
        #[cfg(target_os = "linux")]
        {
            use std::os::linux::net::SocketAddrExt;
            let address = std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes())
                .map_err(|_| "invalid broker socket name")?;
            UnixStream::connect_addr(&address)
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = name;
            return Err("abstract role sockets require Linux".into());
        }
    } else {
        UnixStream::connect(&config.socket)
    }
    .map_err(|_| "role broker is unavailable")?;
    Ok(stream)
}

fn send_command(mut stream: UnixStream, request: serde_json::Value) -> Result<Output, String> {
    let mut bytes = serde_json::to_vec(&request).map_err(|_| "broker request cannot be encoded")?;
    // Role instructions and Forgejo context can exceed the old 128 KiB cap.
    // Keep the request bounded while allowing the full assigned instruction.
    if bytes.len() > 4 * 1024 * 1024 {
        return Err("role command exceeds request limit".into());
    }
    bytes.push(b'\n');
    stream
        .write_all(&bytes)
        .map_err(|_| "broker request failed")?;
    let mut reader = BufReader::new(stream);
    let mut response = Vec::new();
    // JSON escaping can expand the bounded broker output by up to six times.
    use std::io::Read;
    reader
        .by_ref()
        .take(7 * 1048576)
        .read_until(b'\n', &mut response)
        .map_err(|_| "broker response unavailable; command cleanup required")?;
    let output: Output =
        serde_json::from_slice(&response).map_err(|_| "role command refused by broker")?;
    if !output.cleanup_confirmed {
        return Err("role command cleanup could not be verified".into());
    }
    Ok(output)
}

/// Translate controller-side absolute workspace arguments into role-side paths.
pub fn translate_args(cwd: &Path, args: &[&str]) -> Result<Vec<String>, String> {
    let config = config()?;
    let (role, inside) = role_for(&config, cwd)?;
    let root = &config.roles[role].root;
    args.iter()
        .map(|arg| Ok(translate_arg(root, cwd, &inside, arg)))
        .collect()
}

fn translate_arg(root: &Path, cwd: &Path, inside: &Path, arg: &str) -> String {
    let path = Path::new(arg);
    if !path.is_absolute() { return arg.to_owned(); }
    if let Ok(relative) = path.strip_prefix(root) {
        return Path::new("/").join(relative).display().to_string();
    }
    // A runtime workspace may be an authorized symlink to the registered root.
    // New clone destinations do not exist yet, so canonicalize() cannot map
    // them. role_for() has already verified cwd's boundary and identity.
    if let Ok(relative) = path.strip_prefix(cwd) {
        return inside.join(relative).display().to_string();
    }
    arg.to_owned()
}

#[cfg(test)]
mod argument_tests {
    use super::*;
    #[test]
    fn new_clone_under_runtime_alias_uses_verified_inside_cwd() {
        let root=Path::new("/registered/author-01");
        let alias=Path::new("/runtime/agents/author-01/workspace");
        assert_eq!(translate_arg(root,alias,Path::new("/workspace"),"/runtime/agents/author-01/workspace/new.gewu-clone-pending"),"/workspace/new.gewu-clone-pending");
        assert_eq!(translate_arg(root,alias,Path::new("/workspace"),"/registered/author-01/workspace/repo"),"/workspace/repo");
        assert_eq!(translate_arg(root,alias,Path::new("/workspace"),"/runtime/agents/author-02/workspace/repo"),"/runtime/agents/author-02/workspace/repo");
        assert_eq!(translate_arg(root,alias,Path::new("/workspace"),"https://host/team/repo.git"),"https://host/team/repo.git");
    }
}
