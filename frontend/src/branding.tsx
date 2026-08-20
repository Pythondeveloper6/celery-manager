import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { DEFAULT_BRAND_SETTINGS, getSettings } from "./api/settings";
import type { BrandSettings } from "./api/settings";
import {
  THEME_LABELS,
  THEME_PREFERENCES,
  setThemePreference,
  useResolvedTheme,
  useThemePreference,
} from "./theme";
import type { ResolvedTheme } from "./theme";

/* ==========================================================================
   Appearance: deployment branding, and the control for the per-browser theme.
   ==========================================================================

   Two scopes, one file, because they are the two things that decide what the
   dashboard looks like and they must not be confused for each other:

     * branding (name, accent, logo) is one row in the deployment's database —
       change it and every colleague sees the change;
     * the theme preference is one key in this browser's localStorage — change
       it and nobody else is affected.

   The theme *store* lives in ./theme (no React, usable before render); what
   lives here is the switch that drives it, so the nav and the settings screen
   import one component from one place and can't drift apart.
   ========================================================================== */

/* --------------------------------------------------------------------------
   Accent derivation
   --------------------------------------------------------------------------
   The design system doesn't have "an accent colour", it has five tokens that
   have to agree with each other: a fill, a hover and an active step for that
   fill, the text colour that sits *on* the fill, and a soft tint used as a
   background. An operator supplies one hex value. Everything below turns that
   one value into the five, measuring rather than guessing, so a brand colour
   can't quietly push the dashboard below WCAG AA.
   -------------------------------------------------------------------------- */

interface Rgb {
  r: number;
  g: number;
  b: number;
}

const BLACK: Rgb = { r: 0, g: 0, b: 0 };
const WHITE: Rgb = { r: 255, g: 255, b: 255 };

/** WCAG 2.x AA for normal-size text. */
const AA_TEXT = 4.5;

/**
 * The worst backdrop accent-coloured *text* can land on in each theme, taken
 * from tokens.css: in light mode the darkest of the three light surfaces
 * (--cm-color-surface-sunken), in dark mode the lightest of the four dark ones
 * (--cm-color-surface). Deriving against the worst case means the derived
 * colour clears AA on every surface in that theme, not just the common one.
 */
const TEXT_BACKDROP: Record<ResolvedTheme, Rgb> = {
  light: { r: 0xea, g: 0xed, b: 0xef },
  dark: { r: 0x12, g: 0x18, b: 0x20 },
};

/** `#rgb` or `#rrggbb`, matching what the server accepts, or null. */
export function parseHexColour(value: string): Rgb | null {
  const match = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value.trim());
  if (!match) return null;
  const digits =
    match[1].length === 3
      ? match[1]
          .split("")
          .map((digit) => digit + digit)
          .join("")
      : match[1];
  return {
    r: parseInt(digits.slice(0, 2), 16),
    g: parseInt(digits.slice(2, 4), 16),
    b: parseInt(digits.slice(4, 6), 16),
  };
}

function toHex({ r, g, b }: Rgb): string {
  return `#${[r, g, b].map((channel) => channel.toString(16).padStart(2, "0")).join("")}`;
}

function toRgba({ r, g, b }: Rgb, alpha: number): string {
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** WCAG relative luminance: sRGB channel, linearised, weighted for the eye. */
function relativeLuminance({ r, g, b }: Rgb): number {
  const linear = (channel: number): number => {
    const c = channel / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
}

/** WCAG contrast ratio, 1:1 (identical) to 21:1 (black on white). */
export function contrastRatio(a: Rgb, b: Rgb): number {
  const [lighter, darker] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (lighter + 0.05) / (darker + 0.05);
}

/** Straight sRGB interpolation. `amount` 0 keeps `colour`, 1 returns `target`. */
function mix(colour: Rgb, target: Rgb, amount: number): Rgb {
  const blend = (from: number, to: number): number => Math.round(from + (to - from) * amount);
  return { r: blend(colour.r, target.r), g: blend(colour.g, target.g), b: blend(colour.b, target.b) };
}

/**
 * Step `colour` towards `target` (black or white) until it clears AA against
 * `backdrop`, and return the first step that does.
 *
 * Terminates by construction: the last step *is* `target`, and both endpoints
 * clear AA against both backdrops (black on #eaedef is 14.7:1, white on
 * #121820 is 15.9:1), so the loop always has an answer. Small steps keep as
 * much of the operator's colour as the contrast requirement allows — a brand
 * that already passes is returned untouched at step 0.
 */
function darkenUntilReadable(colour: Rgb, target: Rgb, backdrop: Rgb): Rgb {
  for (let step = 0; step < 25; step++) {
    const candidate = mix(colour, target, step * 0.04);
    if (contrastRatio(candidate, backdrop) >= AA_TEXT) return candidate;
  }
  return target;
}

export interface DerivedAccent {
  /** CSS custom properties to set on an element, overriding the built-ins. */
  variables: Record<string, string>;
  /** The brand colour itself, used as the fill. */
  fill: string;
  /** Black or white, whichever reads better on the fill. */
  fillText: string;
  /** Measured contrast of `fillText` on `fill`. Never below 4.58:1. */
  fillRatio: number;
  /** The brand colour adjusted until it works as text on this theme's surfaces. */
  linkText: string;
  /** Measured contrast of `linkText` on the worst surface in this theme. */
  linkRatio: number;
}

/**
 * Turn one brand hex into the accent token set, for one resolved theme.
 *
 * Returns null when there is nothing to derive (empty value, or a string the
 * server would have rejected anyway) — callers then leave the built-in
 * phosphor-cyan tokens alone rather than applying a broken palette.
 *
 * The five decisions, and why:
 *
 *  1. `--cm-color-accent` is the brand colour, untouched. It is a fill; the
 *     operator picked it and should get it.
 *  2. `--cm-color-accent-contrast` is black or white, whichever measures
 *     higher against the fill. The two curves cross at luminance 0.179, where
 *     both give 4.58:1 — so this choice guarantees at least 4.58:1 for *any*
 *     brand colour, AA for normal text with no exceptions.
 *  3. `-hover` / `-active` step the fill 14% and 28% towards the *opposite* of
 *     the colour chosen in (2): a fill with white text gets darker, a fill
 *     with black text gets lighter. That direction is not cosmetic — it means
 *     both interaction states can only ever measure *better* than the resting
 *     state, so hover can never be the thing that fails contrast.
 *  4. `--cm-color-accent-text` is the brand colour walked towards black (light
 *     theme) or white (dark theme) in 4% steps until it clears 4.5:1 against
 *     the worst surface in that theme. A pale brand is perfectly usable as a
 *     button fill and unreadable as link text; splitting the two tokens is
 *     what lets both be true at once. `-text-hover` is one further step in the
 *     same direction, so hovering a link always increases contrast.
 *  5. `--cm-color-accent-soft` is the brand at 14% alpha rather than a
 *     pre-mixed pale tint, because the same token has to work over white in
 *     light mode and over near-black in dark mode. An alpha wash composites
 *     against whatever is behind it and barely moves the luminance of the
 *     surface, so text sitting on it keeps the contrast it had.
 *
 * The focus ring is derived from (4) rather than the raw brand colour: a ring
 * is a visibility affordance, and a pale ring on a pale surface is not one.
 */
export function deriveAccent(accent: string, theme: ResolvedTheme): DerivedAccent | null {
  const brand = parseHexColour(accent);
  if (!brand) return null;

  const onWhite = contrastRatio(brand, WHITE);
  const onBlack = contrastRatio(brand, BLACK);
  const fillText = onWhite >= onBlack ? WHITE : BLACK;
  // Away from the text colour: strictly more contrast, never less.
  const fillStep = onWhite >= onBlack ? BLACK : WHITE;

  const backdrop = TEXT_BACKDROP[theme];
  const textTarget = theme === "light" ? BLACK : WHITE;
  const linkText = darkenUntilReadable(brand, textTarget, backdrop);
  const linkTextHover = mix(linkText, textTarget, 0.14);

  return {
    variables: {
      "--cm-color-accent": toHex(brand),
      "--cm-color-accent-hover": toHex(mix(brand, fillStep, 0.14)),
      "--cm-color-accent-active": toHex(mix(brand, fillStep, 0.28)),
      "--cm-color-accent-contrast": toHex(fillText),
      "--cm-color-accent-soft": toRgba(brand, 0.14),
      "--cm-color-accent-text": toHex(linkText),
      "--cm-color-accent-text-hover": toHex(linkTextHover),
      "--cm-focus-ring": `0 0 0 3px ${toRgba(linkText, 0.4)}`,
    },
    fill: toHex(brand),
    fillText: toHex(fillText),
    fillRatio: Math.max(onWhite, onBlack),
    linkText: toHex(linkText),
    linkRatio: contrastRatio(linkText, backdrop),
  };
}

/* --------------------------------------------------------------------------
   Branding context
   -------------------------------------------------------------------------- */

export type BrandingStatus = "loading" | "ready" | "error";

interface BrandingContextValue {
  settings: BrandSettings;
  status: BrandingStatus;
  /** Why the load failed, for the settings screen. Null unless status is "error". */
  error: string | null;
  /** Re-run the load. Wired to the settings screen's "Try again". */
  refresh: () => void;
  /** Adopt the settings the server returned from a save, app-wide, at once. */
  adopt: (next: BrandSettings) => void;
}

const BrandingContext = createContext<BrandingContextValue | null>(null);

/**
 * One fetch, at the root, shared through context.
 *
 * Context rather than a `useBrandSettings()` hook per component: the wordmark
 * appears in the nav, on the login card, in the browser tab and on the
 * settings screen, and a hook that fetched per caller would issue four
 * requests for one unchanging row and let them disagree with each other while
 * they landed. It also has to sit *above* the authentication check in App, so
 * the login screen is branded — which is the whole reason GET /api/settings is
 * unauthenticated.
 *
 * Failure is deliberately silent everywhere except the settings screen: this
 * provider renders its children immediately with the built-in defaults and
 * upgrades them if and when the response arrives. Branding is cosmetic, and a
 * cosmetic endpoint being down must not be able to stop anyone signing in.
 */
export function BrandingProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<BrandSettings>(DEFAULT_BRAND_SETTINGS);
  const [status, setStatus] = useState<BrandingStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const resolvedTheme = useResolvedTheme();

  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    setError(null);
    getSettings()
      .then((next) => {
        if (cancelled) return;
        setSettings(next);
        setStatus("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // Keep whatever settings we already have — on a retry that failed,
        // that's the last good values, not a regression to the defaults.
        setError(err instanceof Error ? err.message : "Couldn't load settings.");
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const refresh = useCallback(() => setAttempt((n) => n + 1), []);

  const adopt = useCallback((next: BrandSettings) => {
    setSettings(next);
    setStatus("ready");
    setError(null);
  }, []);

  // The browser tab is part of the wordmark. A blank name can't reach here
  // (the server resolves empty to its default) but the guard costs nothing and
  // an empty tab title is a conspicuous way to be wrong.
  useEffect(() => {
    document.title = settings.brand_name || DEFAULT_BRAND_SETTINGS.brand_name;
  }, [settings.brand_name]);

  // Custom accent, applied as inline custom properties on <html> so it
  // overrides the tokens from either theme without the stylesheet knowing
  // anything about branding. Re-derived when the resolved theme changes,
  // because "readable as text" means something different on #eaedef and
  // #121820. The cleanup removes exactly the properties it set, so clearing
  // the accent restores the built-in palette with no reload.
  useEffect(() => {
    const derived = deriveAccent(settings.brand_accent, resolvedTheme);
    if (!derived) return;
    const root = document.documentElement;
    const names = Object.keys(derived.variables);
    for (const name of names) root.style.setProperty(name, derived.variables[name]);
    return () => {
      for (const name of names) root.style.removeProperty(name);
    };
  }, [settings.brand_accent, resolvedTheme]);

  const value = useMemo<BrandingContextValue>(
    () => ({ settings, status, error, refresh, adopt }),
    [settings, status, error, refresh, adopt]
  );

  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>;
}

export function useBranding(): BrandingContextValue {
  const value = useContext(BrandingContext);
  if (!value) {
    // A component rendered outside the provider would silently show the stock
    // wordmark forever, which is exactly the kind of bug that ships. Fail loudly.
    throw new Error("useBranding must be used inside <BrandingProvider>");
  }
  return value;
}

/**
 * The brand dot, or the deployment's logo where one is configured.
 *
 * `alt=""`: every place this renders, the brand name is right next to it as
 * real text, so an alt would make a screen reader say the name twice.
 * A logo URL that 404s falls back to the dot rather than leaving a broken
 * image icon in the nav — an operator's typo shouldn't look like a broken app.
 */
export function BrandMark({ large = false }: { large?: boolean }) {
  const { settings } = useBranding();
  const url = settings.brand_logo_url;
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [url]);

  if (!url || failed) return <span className="cm-brand-mark" aria-hidden="true" />;
  return (
    <img
      className={large ? "cm-brand-logo cm-brand-logo-lg" : "cm-brand-logo"}
      src={url}
      alt=""
      onError={() => setFailed(true)}
    />
  );
}

/**
 * The theme switch: a three-position selector, not a toggle.
 *
 * A single icon button would hide two of the three states and give no way to
 * get back to "follow the OS" once you'd left it. Buttons with `aria-pressed`
 * (the convention the filter bar already uses) rather than a radio group, so
 * every option is reachable with Tab and activated with Enter or Space, and
 * the group's label names what is being chosen.
 */
export function ThemeSwitch() {
  const preference = useThemePreference();
  return (
    <div className="cm-segmented" role="group" aria-label="Theme">
      {THEME_PREFERENCES.map((option) => (
        <button
          key={option}
          type="button"
          className="cm-segmented-option"
          aria-pressed={preference === option}
          aria-label={`${THEME_LABELS[option]} theme`}
          onClick={() => setThemePreference(option)}
        >
          {THEME_LABELS[option]}
        </button>
      ))}
    </div>
  );
}
