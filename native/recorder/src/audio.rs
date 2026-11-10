// SPDX-License-Identifier: GPL-3.0-only
//! Event-driven PipeWire inventory; GSR owns all audio capture and graph links.
use anyhow::{Context, Result};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
};
#[derive(Default)]
pub struct GraphState {
    pub nodes: BTreeMap<u64, Value>,
    pub revision: u64,
    pub error: String,
}
pub struct Monitor {
    child: Child,
    pub state: Arc<Mutex<GraphState>>,
}
impl Monitor {
    pub fn new() -> Result<Self> {
        let mut child = Command::new("pw-dump")
            .arg("--monitor")
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .context("Start PipeWire inventory")?;
        let pipe = child.stdout.take().unwrap();
        let state = Arc::new(Mutex::new(GraphState::default()));
        let s = state.clone();
        thread::spawn(move || {
            for result in serde_json::Deserializer::from_reader(pipe).into_iter::<Value>() {
                match result {
                    Ok(v) => {
                        let mut graph = s.lock().unwrap();
                        if let Some(items) = v.as_array() {
                            for item in items {
                                let Some(id) = item["id"].as_u64() else {
                                    continue;
                                };
                                if item["info"].is_null() {
                                    graph.nodes.remove(&id);
                                } else if item["type"] == "PipeWire:Interface:Node"
                                    || graph.nodes.contains_key(&id)
                                {
                                    let entry=graph.nodes.entry(id).or_insert_with(||json!({"id":id,"type":"PipeWire:Interface:Node","info":{}}));
                                    merge(entry, item);
                                }
                            }
                            graph.revision += 1;
                        }
                    }
                    Err(e) => {
                        s.lock().unwrap().error = format!("PipeWire inventory: {e}");
                        return;
                    }
                }
            }
            s.lock().unwrap().error =
                "PipeWire inventory stopped; existing captured media is retained.".into();
        });
        Ok(Self { child, state })
    }
}
impl Drop for Monitor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
fn merge(a: &mut Value, b: &Value) {
    if let (Some(a), Some(b)) = (a.as_object_mut(), b.as_object()) {
        for (k, v) in b {
            if v.is_object() {
                merge(a.entry(k).or_insert(json!({})), v);
            } else {
                a.insert(k.clone(), v.clone());
            }
        }
    }
}
#[derive(Clone, Debug)]
pub struct Group {
    pub key: String,
    pub meta: Value,
    pub nodes: Vec<u64>,
    pub active: bool,
    pub selector: String,
}
fn text<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn label(name: &str) -> String {
    let s = name.trim();
    let lower = s.to_lowercase();
    let alias = match lower.as_str() {
        "firefox" | "firefox-bin" | "org.mozilla.firefox" => Some("Firefox"),
        "discord"
        | "discordcanary"
        | "discordptb"
        | "com.discordapp.discord"
        | "vesktop"
        | "dev.vencord.vesktop" => Some("Discord"),
        "chromium" | "chromium-browser" => Some("Chromium"),
        "google-chrome" | "google-chrome-stable" => Some("Chrome"),
        "brave-browser" => Some("Brave"),
        "sober" | "org.vinegarhq.sober" => Some("Sober"),
        "genshinimpact.exe" => Some("Genshin Impact"),
        _ => None,
    };
    if let Some(name) = alias {
        return name.into();
    }
    let s = if s.contains('.') && !lower.ends_with(".exe") {
        s.rsplit('.').next().unwrap_or(s)
    } else {
        s
    };
    s.chars()
        .map(|c| {
            if c.is_control() || "\\/<>:\"|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .take(80)
        .collect::<String>()
        .trim_matches([' ', '.'])
        .to_string()
}
pub fn groups(nodes: &BTreeMap<u64, Value>) -> Vec<Group> {
    let mut groups: BTreeMap<String, Group> = BTreeMap::new();
    for (id, node) in nodes {
        let p = &node["info"]["props"];
        let node_name = text(p, "node.name");
        let mut app = text(p, "application.name");
        if app.is_empty() {
            app = text(p, "application.process.binary");
        }
        if app.is_empty() {
            app = node_name;
        }
        if node_name.starts_with("katman-")
            || [
                "layer",
                "katman",
                "layer capture",
                "katman capture",
                "layer studio",
                "gpu screen recorder",
            ]
            .contains(&app.to_lowercase().as_str())
        {
            continue;
        }
        let class = text(p, "media.class");
        let (key, meta, selector) = if class == "Stream/Output/Audio" {
            let discord = [
                "application.name",
                "application.id",
                "application.process.binary",
            ]
            .iter()
            .any(|k| {
                [
                    "discord",
                    "discordcanary",
                    "discordptb",
                    "vesktop",
                    "com.discordapp.discord",
                    "dev.vencord.vesktop",
                ]
                .contains(&text(p, k).to_lowercase().as_str())
            });
            let app = if discord {
                "Discord".to_string()
            } else {
                label(app)
            };
            if app.is_empty() {
                continue;
            }
            let hint = text(p, "media.role").to_lowercase();
            let technical = format!("{} {}", text(p, "media.name"), node_name).to_lowercase();
            let (role, role_label) = if ["communication", "phone", "voip"].contains(&hint.as_str())
            {
                ("voice", "Voice chat")
            } else if ["game", "gaming"].contains(&hint.as_str()) {
                ("game", "Game audio")
            } else if ["notification", "event", "alert"].contains(&hint.as_str()) {
                ("notifications", "Notifications")
            } else if ["music", "movie", "video", "production"].contains(&hint.as_str()) {
                ("media", "Media")
            } else if technical.contains("voiceengine")
                || technical.contains("voice chat")
                || technical.contains("webrtc")
            {
                ("voice", "Voice chat")
            } else if technical.contains("notification") || technical.contains("event-sound") {
                ("notifications", "Notifications")
            } else {
                ("media", "Media")
            };
            (
                format!("app:{}:{role}", app.to_lowercase()),
                json!({"type":"audio","name":format!("{app} — {role_label}"),"kind":"application","application":app,"stream_role":role,"backend":"GPU Screen Recorder","audio_codec":"opus"}),
                format!("app:layer-node:{id}"),
            )
        } else if class == "Audio/Source" || class == "Audio/Source/Virtual" {
            let description = text(p, "node.description");
            (
                format!("mic:{node_name}"),
                json!({"type":"audio","name":if description.is_empty(){app}else{description},"kind":"microphone","backend":"GPU Screen Recorder","audio_codec":"opus"}),
                format!("device:{node_name}"),
            )
        } else {
            continue;
        };
        let active = class != "Stream/Output/Audio" || text(&node["info"], "state") == "running";
        let g = groups.entry(key.clone()).or_insert_with(|| Group {
            key,
            meta,
            nodes: Vec::new(),
            active: false,
            selector: String::new(),
        });
        g.nodes.push(*id);
        g.active |= active;
        if !g.selector.is_empty() {
            g.selector.push('|');
        }
        g.selector.push_str(&selector);
    }
    groups.into_values().collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roles_remain_independent() {
        let mut n = BTreeMap::new();
        for (id, role) in [(1, "Communication"), (2, "Movie")] {
            n.insert(id,json!({"id":id,"info":{"state":"running","props":{"media.class":"Stream/Output/Audio","application.name":"Discord","media.role":role,"node.name":"discord-stream"}}}));
        }
        let g = groups(&n);
        assert_eq!(g.len(), 2);
        assert!(
            g.iter()
                .any(|g| g.key == "app:discord:voice" && g.selector == "app:layer-node:1")
        );
        assert!(
            g.iter()
                .any(|g| g.key == "app:discord:media" && g.selector == "app:layer-node:2")
        );
    }
    #[test]
    fn explicit_roles_outrank_media_titles() {
        let mut nodes = BTreeMap::new();
        nodes.insert(1,json!({"info":{"props":{"media.class":"Stream/Output/Audio","application.name":"Discord","media.role":"Movie","media.name":"Private voice chat notification title","node.name":"discord"}}}));
        assert_eq!(groups(&nodes)[0].key, "app:discord:media");
    }
    #[test]
    fn incremental_props_keep_identity() {
        let mut a = json!({"id":1,"info":{"props":{"application.name":"Firefox"},"state":"idle"}});
        merge(&mut a, &json!({"id":1,"info":{"state":"running"}}));
        assert_eq!(a["info"]["props"]["application.name"], "Firefox");
        assert_eq!(a["info"]["state"], "running");
    }
    #[test]
    fn names_never_use_titles() {
        assert_eq!(label("org.mozilla.firefox"), "Firefox");
        assert_eq!(label("a/b"), "a_b");
        assert_eq!(label(".."), "");
    }
}
