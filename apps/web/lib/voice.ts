"use client";

/**
 * Voice I/O using the browser's Web Speech API (free, no keys).
 *  - Speech-to-text: SpeechRecognition (Chrome, Edge, Safari). Not available in Firefox.
 *  - Text-to-speech: speechSynthesis with per-agent voice profiles (original system voices, no cloning).
 * Server-side STT/TTS provider adapters are planned (see docs/IMPLEMENTATION_STATUS.md).
 */

export interface VoiceProfile {
  lang: string;
  pitch: number;
  rate: number;
  preferred_voices: string[];
  /** Muted agents (U. is mute) are never spoken aloud. */
  muted?: boolean;
}

export interface VoiceLike {
  name: string;
  lang: string;
}

/** Pick the best available system voice for a profile: preferred name > exact lang > language family > any. */
export function pickVoice<T extends VoiceLike>(voices: T[], profile: VoiceProfile): T | null {
  if (!voices.length) return null;
  for (const pref of profile.preferred_voices) {
    const v = voices.find((x) => x.name.toLowerCase().includes(pref.toLowerCase()));
    if (v) return v;
  }
  const lang = profile.lang.toLowerCase();
  const exact = voices.find((x) => x.lang.toLowerCase() === lang);
  if (exact) return exact;
  const family = voices.find((x) => x.lang.toLowerCase().startsWith(lang.split("-")[0]));
  return family || voices[0];
}

/** Strip markdown so it is read naturally. */
export function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " (code omitted) ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/https?:\/\/\S+/g, "link")
    .replace(/[#*_>|]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 1500);
}

export function ttsSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export function sttSupported(): boolean {
  return typeof window !== "undefined" && !!((window as any).SpeechRecognition || (window as any).webkitSpeechRecognition);
}

/** Time the dashboard last finished speaking, so the microphone can ignore its own voice. */
let lastSpokeAt = 0;

/** True while the dashboard is speaking (and briefly after), so wake-word mode ignores its own replies. */
export function isSpeaking(): boolean {
  if (!ttsSupported()) return false;
  return window.speechSynthesis.speaking || Date.now() - lastSpokeAt < 1500;
}

export function speak(text: string, profile: VoiceProfile, onEnd?: () => void): void {
  if (profile.muted || !ttsSupported()) {
    onEnd?.();
    return;
  }
  const synth = window.speechSynthesis;
  synth.cancel(); // interrupt anything already speaking
  const u = new SpeechSynthesisUtterance(speakable(text));
  const voice = pickVoice(synth.getVoices(), profile);
  if (voice) u.voice = voice;
  u.lang = voice?.lang || profile.lang;
  u.pitch = Math.max(0, Math.min(2, profile.pitch));
  u.rate = Math.max(0.5, Math.min(2, profile.rate));
  u.onend = () => {
    lastSpokeAt = Date.now();
    onEnd?.();
  };
  u.onerror = () => {
    lastSpokeAt = Date.now();
    onEnd?.();
  };
  synth.speak(u);
}

export function stopSpeaking(): void {
  if (ttsSupported()) window.speechSynthesis.cancel();
}

export interface Recognizer {
  start(): void;
  stop(): void;
}

export function createRecognizer(opts: {
  lang?: string;
  continuous?: boolean;
  onPartial?: (text: string) => void;
  onFinal: (text: string) => void;
  onState?: (listening: boolean) => void;
  onError?: (message: string) => void;
}): Recognizer | null {
  if (!sttSupported()) return null;
  const Ctor = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
  const rec = new Ctor();
  rec.lang = opts.lang || "en-US";
  rec.interimResults = true;
  rec.continuous = !!opts.continuous;
  let active = false;
  rec.onstart = () => {
    active = true;
    opts.onState?.(true);
  };
  let restartDelay = 0;
  rec.onend = () => {
    opts.onState?.(false);
    if (active && opts.continuous) {
      // Restart to keep wake-word mode alive; back off after network errors instead of looping.
      setTimeout(() => {
        if (!active) return;
        try {
          rec.start();
        } catch {
          /* already started */
        }
      }, restartDelay);
    }
  };
  rec.onerror = (e: any) => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") active = false;
    restartDelay = e.error === "network" ? Math.min(30000, Math.max(2000, restartDelay * 2)) : 0;
    // Silence and our own restarts are normal in continuous mode: don't flood the screen with toasts.
    if (opts.continuous && (e.error === "no-speech" || e.error === "aborted")) return;
    if (opts.continuous && e.error === "network" && restartDelay > 2000) return; // already reported once
    opts.onError?.(e.error === "not-allowed" ? "Microphone permission denied" : `Speech error: ${e.error}`);
  };
  rec.onresult = (e: any) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) opts.onFinal(r[0].transcript.trim());
      else interim += r[0].transcript;
    }
    if (interim) opts.onPartial?.(interim);
  };
  return {
    start() {
      active = true;
      try {
        rec.start();
      } catch {
        /* already running */
      }
    },
    stop() {
      active = false;
      try {
        rec.stop();
      } catch {
        /* not running */
      }
    },
  };
}
