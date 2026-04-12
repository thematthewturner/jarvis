import { useState, useEffect, useRef } from "react";

const JARVIS_TOKENS = {
  // Core palette
  "--jarvis-void": "#0a0c10",
  "--jarvis-plate": "#12151c",
  "--jarvis-panel": "#1a1e2a",
  "--jarvis-panel-hover": "#222738",
  
  // Arc Reactor Blue (primary)
  "--jarvis-arc": "#4fc3f7",
  "--jarvis-arc-bright": "#81d4fa",
  "--jarvis-arc-dim": "#0288d1",
  "--jarvis-arc-glow": "rgba(79, 195, 247, 0.15)",
  "--jarvis-arc-pulse": "rgba(79, 195, 247, 0.08)",
  
  // Stark Gold (accent)
  "--jarvis-gold": "#ffc107",
  "--jarvis-gold-bright": "#ffd54f",
  "--jarvis-gold-dim": "#ff8f00",
  "--jarvis-gold-glow": "rgba(255, 193, 7, 0.12)",
  
  // HUD Red (warning/danger)
  "--jarvis-red": "#ef5350",
  "--jarvis-red-bright": "#ff8a80",
  "--jarvis-red-dim": "#c62828",
  "--jarvis-red-glow": "rgba(239, 83, 80, 0.12)",
  
  // Status Green (success)
  "--jarvis-green": "#66bb6a",
  "--jarvis-green-bright": "#a5d6a7",
  "--jarvis-green-dim": "#2e7d32",
  
  // Text hierarchy
  "--jarvis-text": "#e1e8f0",
  "--jarvis-text-muted": "#8899aa",
  "--jarvis-text-dim": "#556677",
  
  // Borders & dividers
  "--jarvis-border": "rgba(79, 195, 247, 0.12)",
  "--jarvis-border-active": "rgba(79, 195, 247, 0.35)",
  "--jarvis-divider": "rgba(255, 255, 255, 0.06)",
};

const CSS_EXPORT = `:root {
  /* ═══════════════════════════════════════════
     JARVIS — Iron Man HUD Color System
     ═══════════════════════════════════════════ */

  /* Core Surfaces */
  --jarvis-void:        #0a0c10;   /* deepest background          */
  --jarvis-plate:       #12151c;   /* card/panel background        */
  --jarvis-panel:       #1a1e2a;   /* elevated surfaces            */
  --jarvis-panel-hover: #222738;   /* interactive hover state      */

  /* Arc Reactor Blue — Primary */
  --jarvis-arc:         #4fc3f7;   /* primary actions & highlights */
  --jarvis-arc-bright:  #81d4fa;   /* hover / active states        */
  --jarvis-arc-dim:     #0288d1;   /* pressed / visited states     */
  --jarvis-arc-glow:    rgba(79, 195, 247, 0.15);  /* ambient glow */
  --jarvis-arc-pulse:   rgba(79, 195, 247, 0.08);  /* subtle pulse */

  /* Stark Gold — Accent */
  --jarvis-gold:        #ffc107;   /* highlights & badges          */
  --jarvis-gold-bright: #ffd54f;   /* hover state                  */
  --jarvis-gold-dim:    #ff8f00;   /* pressed state                */
  --jarvis-gold-glow:   rgba(255, 193, 7, 0.12);   /* ambient glow */

  /* HUD Red — Warning / Danger */
  --jarvis-red:         #ef5350;   /* errors & alerts              */
  --jarvis-red-bright:  #ff8a80;   /* hover state                  */
  --jarvis-red-dim:     #c62828;   /* pressed state                */
  --jarvis-red-glow:    rgba(239, 83, 80, 0.12);   /* ambient glow */

  /* Status Green — Success */
  --jarvis-green:       #66bb6a;   /* success indicators           */
  --jarvis-green-bright:#a5d6a7;   /* hover state                  */
  --jarvis-green-dim:   #2e7d32;   /* pressed state                */

  /* Text Hierarchy */
  --jarvis-text:        #e1e8f0;   /* primary text                 */
  --jarvis-text-muted:  #8899aa;   /* secondary / labels           */
  --jarvis-text-dim:    #556677;   /* disabled / tertiary          */

  /* Borders & Dividers */
  --jarvis-border:        rgba(79, 195, 247, 0.12);
  --jarvis-border-active: rgba(79, 195, 247, 0.35);
  --jarvis-divider:       rgba(255, 255, 255, 0.06);

  /* Utility Gradients */
  --jarvis-gradient-arc:  linear-gradient(135deg, #0288d1, #4fc3f7);
  --jarvis-gradient-gold: linear-gradient(135deg, #ff8f00, #ffc107);
  --jarvis-gradient-hud:  linear-gradient(180deg, rgba(79,195,247,0.05) 0%, transparent 60%);

  /* Glow Shadows */
  --jarvis-shadow-arc:  0 0 20px rgba(79, 195, 247, 0.25), 0 0 60px rgba(79, 195, 247, 0.1);
  --jarvis-shadow-gold: 0 0 20px rgba(255, 193, 7, 0.2), 0 0 60px rgba(255, 193, 7, 0.08);
  --jarvis-shadow-red:  0 0 20px rgba(239, 83, 80, 0.2), 0 0 60px rgba(239, 83, 80, 0.08);
}`;

function ArcReactor({ size = 120 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 120 120" style={{ filter: "drop-shadow(0 0 20px rgba(79,195,247,0.5))" }}>
      <defs>
        <radialGradient id="coreGlow" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#81d4fa" stopOpacity="1" />
          <stop offset="40%" stopColor="#4fc3f7" stopOpacity="0.8" />
          <stop offset="100%" stopColor="#0288d1" stopOpacity="0" />
        </radialGradient>
        <filter id="blur">
          <feGaussianBlur stdDeviation="2" />
        </filter>
      </defs>
      <circle cx="60" cy="60" r="55" fill="none" stroke="#1a1e2a" strokeWidth="3" />
      <circle cx="60" cy="60" r="50" fill="none" stroke="rgba(79,195,247,0.15)" strokeWidth="1" />
      <circle cx="60" cy="60" r="45" fill="none" stroke="rgba(79,195,247,0.2)" strokeWidth="0.5" strokeDasharray="4 8" style={{ animation: "spin 20s linear infinite" }} />
      <circle cx="60" cy="60" r="38" fill="none" stroke="rgba(79,195,247,0.25)" strokeWidth="1" strokeDasharray="2 6" style={{ animation: "spin 15s linear infinite reverse" }} />
      {[0, 60, 120, 180, 240, 300].map((angle, i) => (
        <line key={i} x1="60" y1="60" x2={60 + 48 * Math.cos((angle * Math.PI) / 180)} y2={60 + 48 * Math.sin((angle * Math.PI) / 180)} stroke="rgba(79,195,247,0.15)" strokeWidth="0.5" />
      ))}
      <circle cx="60" cy="60" r="28" fill="none" stroke="#4fc3f7" strokeWidth="1.5" opacity="0.6" />
      <circle cx="60" cy="60" r="18" fill="url(#coreGlow)" filter="url(#blur)" style={{ animation: "pulse 3s ease-in-out infinite" }} />
      <circle cx="60" cy="60" r="12" fill="#4fc3f7" opacity="0.9" />
      <circle cx="60" cy="60" r="6" fill="#e1f5fe" />
    </svg>
  );
}

function SwatchGroup({ title, swatches }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.1em", color: "#8899aa", marginBottom: 10 }}>{title}</div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {swatches.map(([name, color]) => (
          <div key={name} style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 6, minWidth: 72 }}>
            <div style={{
              width: 56, height: 56, borderRadius: 10, background: color,
              border: "1px solid rgba(255,255,255,0.08)",
              boxShadow: color.startsWith("rgba") ? "none" : `0 0 16px ${color}33`,
              transition: "transform 0.2s, box-shadow 0.2s",
              cursor: "pointer",
            }}
              onMouseEnter={e => { e.target.style.transform = "scale(1.1)"; e.target.style.boxShadow = `0 0 24px ${color}66`; }}
              onMouseLeave={e => { e.target.style.transform = "scale(1)"; e.target.style.boxShadow = color.startsWith("rgba") ? "none" : `0 0 16px ${color}33`; }}
            />
            <div style={{ fontSize: 9, color: "#556677", textAlign: "center", fontFamily: "'JetBrains Mono', monospace", lineHeight: 1.3 }}>
              <div style={{ color: "#8899aa", fontSize: 10 }}>{name.replace("--jarvis-", "")}</div>
              <div>{typeof color === "string" && color.length < 20 ? color : ""}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function HUDButton({ children, variant = "arc", glow = false }) {
  const colors = {
    arc: { bg: "#4fc3f7", hover: "#81d4fa", shadow: "rgba(79,195,247,0.4)" },
    gold: { bg: "#ffc107", hover: "#ffd54f", shadow: "rgba(255,193,7,0.4)" },
    red: { bg: "#ef5350", hover: "#ff8a80", shadow: "rgba(239,83,80,0.4)" },
    ghost: { bg: "transparent", hover: "rgba(79,195,247,0.1)", shadow: "none" },
  };
  const c = colors[variant];
  const [hovered, setHovered] = useState(false);
  return (
    <button
      onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}
      style={{
        background: variant === "ghost" ? (hovered ? c.hover : c.bg) : hovered ? c.hover : c.bg,
        color: variant === "ghost" ? "#4fc3f7" : variant === "gold" ? "#0a0c10" : "#0a0c10",
        border: variant === "ghost" ? "1px solid rgba(79,195,247,0.3)" : "none",
        padding: "10px 22px", borderRadius: 8, fontSize: 13, fontWeight: 600, fontFamily: "'JetBrains Mono', monospace",
        cursor: "pointer", letterSpacing: "0.04em", textTransform: "uppercase",
        boxShadow: glow && hovered ? `0 0 24px ${c.shadow}` : "none",
        transition: "all 0.2s ease",
      }}
    >{children}</button>
  );
}

function StatusBar() {
  const [time, setTime] = useState(new Date());
  useEffect(() => { const t = setInterval(() => setTime(new Date()), 1000); return () => clearInterval(t); }, []);
  const items = [
    { label: "SYSTEMS", value: "ONLINE", color: "#66bb6a" },
    { label: "CORE TEMP", value: "72°F", color: "#4fc3f7" },
    { label: "POWER", value: "98.7%", color: "#ffc107" },
    { label: "THREATS", value: "0", color: "#66bb6a" },
  ];
  return (
    <div style={{
      display: "flex", gap: 24, padding: "14px 20px", background: "#12151c",
      borderRadius: 10, border: "1px solid rgba(79,195,247,0.12)",
      fontFamily: "'JetBrains Mono', monospace", fontSize: 11,
    }}>
      {items.map(i => (
        <div key={i.label} style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ color: "#556677" }}>{i.label}</span>
          <span style={{ color: i.color, fontWeight: 600 }}>{i.value}</span>
        </div>
      ))}
      <div style={{ marginLeft: "auto", color: "#8899aa" }}>
        {time.toLocaleTimeString("en-US", { hour12: false })}
      </div>
    </div>
  );
}

function CodePreview({ code }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ position: "relative", borderRadius: 10, overflow: "hidden", border: "1px solid rgba(79,195,247,0.12)" }}>
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center",
        padding: "10px 16px", background: "#12151c", borderBottom: "1px solid rgba(255,255,255,0.06)",
      }}>
        <span style={{ fontSize: 11, color: "#556677", fontFamily: "'JetBrains Mono', monospace" }}>jarvis-theme.css</span>
        <button
          onClick={() => { navigator.clipboard.writeText(code).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); }); }}
          style={{
            background: copied ? "rgba(102,187,106,0.15)" : "rgba(79,195,247,0.1)",
            border: "1px solid " + (copied ? "rgba(102,187,106,0.3)" : "rgba(79,195,247,0.2)"),
            color: copied ? "#66bb6a" : "#4fc3f7",
            padding: "4px 12px", borderRadius: 6, fontSize: 11, cursor: "pointer",
            fontFamily: "'JetBrains Mono', monospace", transition: "all 0.2s",
          }}
        >{copied ? "✓ COPIED" : "COPY CSS"}</button>
      </div>
      <pre style={{
        margin: 0, padding: 16, background: "#0a0c10", fontSize: 11, lineHeight: 1.6,
        color: "#8899aa", fontFamily: "'JetBrains Mono', monospace",
        maxHeight: 280, overflow: "auto",
      }}>{code}</pre>
    </div>
  );
}

export default function JarvisTheme() {
  const [activeTab, setActiveTab] = useState("palette");
  const tabs = ["palette", "components", "css"];

  return (
    <div style={{
      minHeight: "100vh", background: "#0a0c10", color: "#e1e8f0",
      fontFamily: "'Outfit', sans-serif", padding: 32, position: "relative", overflow: "hidden",
    }}>
      <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
      <style>{`
        @keyframes spin { from { transform-origin: 60px 60px; transform: rotate(0deg); } to { transform-origin: 60px 60px; transform: rotate(360deg); } }
        @keyframes pulse { 0%, 100% { opacity: 0.7; transform: scale(1); } 50% { opacity: 1; transform: scale(1.05); } }
        @keyframes scanline { 0% { transform: translateY(-100%); } 100% { transform: translateY(100vh); } }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: translateY(0); } }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #12151c; }
        ::-webkit-scrollbar-thumb { background: rgba(79,195,247,0.2); border-radius: 3px; }
      `}</style>

      {/* Scanline overlay */}
      <div style={{
        position: "fixed", top: 0, left: 0, right: 0, bottom: 0, pointerEvents: "none", zIndex: 100,
        background: "repeating-linear-gradient(0deg, rgba(79,195,247,0.015) 0px, transparent 1px, transparent 3px)",
      }} />
      
      {/* Ambient glow */}
      <div style={{
        position: "fixed", top: -200, left: "50%", transform: "translateX(-50%)",
        width: 600, height: 400, borderRadius: "50%",
        background: "radial-gradient(ellipse, rgba(79,195,247,0.06) 0%, transparent 70%)",
        pointerEvents: "none",
      }} />

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 24, marginBottom: 32, animation: "fadeIn 0.6s ease" }}>
        <ArcReactor size={80} />
        <div>
          <div style={{ fontSize: 11, letterSpacing: "0.2em", color: "#4fc3f7", fontFamily: "'JetBrains Mono', monospace", marginBottom: 4, fontWeight: 600 }}>
            STARK INDUSTRIES • DESIGN SYSTEM
          </div>
          <h1 style={{ margin: 0, fontSize: 36, fontWeight: 700, letterSpacing: "-0.02em", lineHeight: 1.1 }}>
            J.A.R.V.I.S.
          </h1>
          <div style={{ fontSize: 13, color: "#556677", marginTop: 4, fontFamily: "'JetBrains Mono', monospace" }}>
            Just A Rather Very Intelligent System — Color Protocol v3.0
          </div>
        </div>
      </div>

      {/* Status bar */}
      <div style={{ marginBottom: 28, animation: "fadeIn 0.6s ease 0.1s both" }}>
        <StatusBar />
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 28, animation: "fadeIn 0.6s ease 0.2s both" }}>
        {tabs.map(t => (
          <button key={t} onClick={() => setActiveTab(t)} style={{
            padding: "10px 20px", borderRadius: 8, border: "1px solid " + (activeTab === t ? "rgba(79,195,247,0.3)" : "transparent"),
            background: activeTab === t ? "rgba(79,195,247,0.1)" : "transparent",
            color: activeTab === t ? "#4fc3f7" : "#556677",
            fontSize: 12, fontWeight: 600, cursor: "pointer", textTransform: "uppercase",
            fontFamily: "'JetBrains Mono', monospace", letterSpacing: "0.08em",
            transition: "all 0.2s",
          }}>{t}</button>
        ))}
      </div>

      {/* Content */}
      <div style={{ animation: "fadeIn 0.4s ease" }}>
        {activeTab === "palette" && (
          <div style={{ display: "grid", gap: 28 }}>
            <SwatchGroup title="Surfaces" swatches={[
              ["--jarvis-void", "#0a0c10"], ["--jarvis-plate", "#12151c"],
              ["--jarvis-panel", "#1a1e2a"], ["--jarvis-panel-hover", "#222738"],
            ]} />
            <SwatchGroup title="Arc Reactor Blue — Primary" swatches={[
              ["--jarvis-arc-dim", "#0288d1"], ["--jarvis-arc", "#4fc3f7"],
              ["--jarvis-arc-bright", "#81d4fa"], ["--jarvis-arc-glow", "rgba(79,195,247,0.15)"],
            ]} />
            <SwatchGroup title="Stark Gold — Accent" swatches={[
              ["--jarvis-gold-dim", "#ff8f00"], ["--jarvis-gold", "#ffc107"],
              ["--jarvis-gold-bright", "#ffd54f"], ["--jarvis-gold-glow", "rgba(255,193,7,0.12)"],
            ]} />
            <SwatchGroup title="HUD Red — Warning" swatches={[
              ["--jarvis-red-dim", "#c62828"], ["--jarvis-red", "#ef5350"],
              ["--jarvis-red-bright", "#ff8a80"], ["--jarvis-red-glow", "rgba(239,83,80,0.12)"],
            ]} />
            <SwatchGroup title="Status Green — Success" swatches={[
              ["--jarvis-green-dim", "#2e7d32"], ["--jarvis-green", "#66bb6a"],
              ["--jarvis-green-bright", "#a5d6a7"],
            ]} />
            <SwatchGroup title="Text Hierarchy" swatches={[
              ["--jarvis-text", "#e1e8f0"], ["--jarvis-text-muted", "#8899aa"],
              ["--jarvis-text-dim", "#556677"],
            ]} />
          </div>
        )}

        {activeTab === "components" && (
          <div style={{ display: "grid", gap: 24 }}>
            <div style={{ padding: 24, background: "#12151c", borderRadius: 12, border: "1px solid rgba(79,195,247,0.12)" }}>
              <div style={{ fontSize: 11, color: "#556677", fontFamily: "'JetBrains Mono', monospace", marginBottom: 16, textTransform: "uppercase", letterSpacing: "0.1em" }}>Buttons</div>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center" }}>
                <HUDButton variant="arc" glow>Initialize</HUDButton>
                <HUDButton variant="gold" glow>Override</HUDButton>
                <HUDButton variant="red" glow>Terminate</HUDButton>
                <HUDButton variant="ghost">Diagnostics</HUDButton>
              </div>
            </div>

            <div style={{ padding: 24, background: "#12151c", borderRadius: 12, border: "1px solid rgba(79,195,247,0.12)" }}>
              <div style={{ fontSize: 11, color: "#556677", fontFamily: "'JetBrains Mono', monospace", marginBottom: 16, textTransform: "uppercase", letterSpacing: "0.1em" }}>Alert Cards</div>
              <div style={{ display: "grid", gap: 12 }}>
                {[
                  { color: "#4fc3f7", bg: "rgba(79,195,247,0.08)", border: "rgba(79,195,247,0.2)", label: "INFO", msg: "All systems nominal. Arc reactor output stable." },
                  { color: "#ffc107", bg: "rgba(255,193,7,0.08)", border: "rgba(255,193,7,0.2)", label: "WARN", msg: "Elevated power draw detected in sector 7-G." },
                  { color: "#ef5350", bg: "rgba(239,83,80,0.08)", border: "rgba(239,83,80,0.2)", label: "CRIT", msg: "Perimeter breach. Initiating countermeasures." },
                  { color: "#66bb6a", bg: "rgba(102,187,106,0.08)", border: "rgba(102,187,106,0.2)", label: "OK", msg: "Threat neutralized. Returning to standby." },
                ].map(a => (
                  <div key={a.label} style={{
                    display: "flex", alignItems: "center", gap: 14, padding: "12px 16px",
                    background: a.bg, borderRadius: 8, borderLeft: `3px solid ${a.color}`,
                  }}>
                    <span style={{ fontSize: 10, fontWeight: 700, color: a.color, fontFamily: "'JetBrains Mono', monospace", minWidth: 36 }}>{a.label}</span>
                    <span style={{ fontSize: 13, color: "#e1e8f0" }}>{a.msg}</span>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ padding: 24, background: "#12151c", borderRadius: 12, border: "1px solid rgba(79,195,247,0.12)" }}>
              <div style={{ fontSize: 11, color: "#556677", fontFamily: "'JetBrains Mono', monospace", marginBottom: 16, textTransform: "uppercase", letterSpacing: "0.1em" }}>Input Field</div>
              <div style={{ position: "relative" }}>
                <input
                  placeholder="Enter command, sir..."
                  style={{
                    width: "100%", padding: "14px 16px", background: "#0a0c10",
                    border: "1px solid rgba(79,195,247,0.2)", borderRadius: 8,
                    color: "#e1e8f0", fontSize: 14, fontFamily: "'JetBrains Mono', monospace",
                    outline: "none", boxSizing: "border-box",
                  }}
                  onFocus={e => { e.target.style.borderColor = "rgba(79,195,247,0.5)"; e.target.style.boxShadow = "0 0 16px rgba(79,195,247,0.1)"; }}
                  onBlur={e => { e.target.style.borderColor = "rgba(79,195,247,0.2)"; e.target.style.boxShadow = "none"; }}
                />
              </div>
            </div>
          </div>
        )}

        {activeTab === "css" && (
          <CodePreview code={CSS_EXPORT} />
        )}
      </div>
    </div>
  );
}
