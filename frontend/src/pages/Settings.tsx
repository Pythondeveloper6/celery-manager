import { useEffect, useId, useMemo, useState } from "react";
import type { CSSProperties, FormEvent } from "react";
import { DEFAULT_BRAND_SETTINGS, updateSettings } from "../api/settings";
import type { BrandSettings } from "../api/settings";
import { BrandGlyph, ThemeSwitch, deriveAccent, parseHexColour, useBranding } from "../branding";
import { IconListChecks } from "../components/icons";
import { useResolvedTheme, useThemePreference } from "../theme";
import { useCan } from "../lib/role";

const BRAND_KEYS = ["brand_name", "brand_accent", "brand_logo_url"] as const;

/**
 * Point the server's rejection at the field it is about.
 *
 * The settings endpoint answers a bad value with one sentence that starts with
 * the key it rejected ("brand_accent must be empty or a hex colour like
 * #0ff or #00ffcc"), so the message can be shown under the field the operator
 * has to fix rather than as a banner they then have to map onto a form. Any
 * message that doesn't name a field — an expired session, an unknown key, a
 * proxy error — falls through to the form-level error, which is why this
 * returns null instead of guessing.
 */
function fieldForMessage(message: string): (typeof BRAND_KEYS)[number] | null {
  return BRAND_KEYS.find((key) => message.startsWith(key)) ?? null;
}

function sameSettings(a: BrandSettings, b: BrandSettings): boolean {
  return BRAND_KEYS.every((key) => a[key] === b[key]);
}

/**
 * The accent currently painted on the page, read back out of CSS rather than
 * repeated here as a hex literal. It seeds the colour picker and the accent
 * field's placeholder, so both start from what the operator is actually
 * looking at — and tokens.css stays the only place the built-in palette is
 * written down.
 */
function readEffectiveAccent(): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue("--cm-color-primary");
  return parseHexColour(value) ? value.trim() : "#000000";
}

export function Settings() {
  const { settings, status, error: loadError, refresh, adopt } = useBranding();
  const resolvedTheme = useResolvedTheme();
  const themePreference = useThemePreference();

  // Branding is deployment-wide, so changing it is admin-only (the server
  // refuses anyone else with a 403). Others see the form read-only.
  const canEdit = useCan("admin");
  const [draft, setDraft] = useState<BrandSettings>(settings);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [logoFailed, setLogoFailed] = useState(false);
  const [effectiveAccent, setEffectiveAccent] = useState("#000000");

  const nameId = useId();
  const accentId = useId();
  const logoId = useId();
  const brandingHeadingId = useId();
  const appearanceHeadingId = useId();

  // Re-seed from the server whenever the shared settings change: once when the
  // initial load lands, and again after a save, so the form shows the values
  // the server actually stored (trimmed, defaults resolved) rather than the
  // ones that were typed.
  useEffect(() => setDraft(settings), [settings]);

  useEffect(() => setLogoFailed(false), [draft.brand_logo_url]);

  useEffect(() => setEffectiveAccent(readEffectiveAccent()), [resolvedTheme, settings.brand_accent]);

  const derived = useMemo(
    () => deriveAccent(draft.brand_accent, resolvedTheme),
    [draft.brand_accent, resolvedTheme]
  );

  const dirty = !sameSettings(draft, settings);
  const isDefault = sameSettings(settings, DEFAULT_BRAND_SETTINGS);
  const accentTyped = draft.brand_accent.trim() !== "";
  const accentUnreadable = accentTyped && derived === null;
  const errorField = saveError ? fieldForMessage(saveError) : null;

  function edit(key: (typeof BRAND_KEYS)[number], value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
    // A confirmation that stays on screen while the form is edited underneath
    // it is describing a state that no longer exists.
    setSaved(false);
    setSaveError(null);
  }

  async function save(values: BrandSettings) {
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      // `adopt` re-brands the whole app — nav wordmark, tab title, accent —
      // from the server's response, so the change is visible everywhere the
      // moment it is stored, with no reload and no second request.
      adopt(await updateSettings(values));
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Couldn't save settings.");
    } finally {
      setSaving(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    void save(draft);
  }

  function handleReset() {
    // Empty strings, not the default values: empty is the API's word for
    // "unset this", and the server resolves it to its own defaults. Sending
    // the strings we think the defaults are would bake this build's idea of
    // them into the deployment's database.
    void save({ brand_name: "", brand_accent: "", brand_logo_url: "" });
  }

  const previewStyle = derived ? (derived.variables as unknown as CSSProperties) : undefined;
  const previewName = draft.brand_name.trim() || DEFAULT_BRAND_SETTINGS.brand_name;
  const previewLogo = draft.brand_logo_url.trim();

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <h1 className="cm-page-title">Settings</h1>
      </header>

      <section className="cm-card cm-settings-section" aria-labelledby={brandingHeadingId}>
        <div className="cm-card-header">
          <h2 className="cm-card-title" id={brandingHeadingId}>
            Branding
          </h2>
          <p className="cm-card-description">
            <span className="cm-scope-tag">Deployment-wide.</span>{" "}
            <span>
              Saved on the server. Everyone who opens this dashboard sees the name, colour and logo you
              set here, including on the sign-in screen.
            </span>
          </p>
        </div>

        {status === "loading" ? (
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading settings&hellip;
          </p>
        ) : status === "error" ? (
          // Not a form seeded with defaults: editing from a state we failed to
          // read would let one save overwrite branding nobody has seen.
          <div className="cm-stack">
            <p className="cm-error" role="alert">
              {loadError ?? "Couldn't load settings."}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : (
          <form className="cm-settings-form" onSubmit={handleSubmit} noValidate>
            {/* Disabled as a set while a save is in flight: the button alone
                would leave three editable inputs whose values are no longer
                the ones being written. */}
            {!canEdit && (
              <p className="cm-field-hint">Only an admin can change branding. You can still pick your own theme below.</p>
            )}
            <fieldset className="cm-fieldset" disabled={saving || !canEdit}>
              <legend className="cm-visually-hidden">Branding</legend>

              <div className="cm-field">
                <label htmlFor={nameId}>Brand name</label>
                <input
                  id={nameId}
                  className="cm-input"
                  value={draft.brand_name}
                  onChange={(e) => edit("brand_name", e.target.value)}
                  maxLength={64}
                  spellCheck={false}
                  aria-describedby={`${nameId}-hint`}
                  aria-invalid={errorField === "brand_name" || undefined}
                />
                <p className="cm-field-hint" id={`${nameId}-hint`}>
                  Up to 64 characters. Shown in the sidebar, on the sign-in screen and in the browser tab.
                  Leave it empty to go back to {DEFAULT_BRAND_SETTINGS.brand_name}.
                </p>
                {errorField === "brand_name" && (
                  <p className="cm-field-error" role="alert">
                    {saveError}
                  </p>
                )}
              </div>

              <div className="cm-field">
                <label htmlFor={accentId}>Accent colour</label>
                <div className="cm-field-inline">
                  <input
                    id={accentId}
                    className="cm-input cm-input-code"
                    value={draft.brand_accent}
                    onChange={(e) => edit("brand_accent", e.target.value)}
                    placeholder={effectiveAccent}
                    spellCheck={false}
                    autoCapitalize="none"
                    aria-describedby={`${accentId}-hint`}
                    aria-invalid={errorField === "brand_accent" || undefined}
                  />
                  {/* Convenience, not the source of truth: the text field is
                      what gets submitted, so an accent can still be pasted or
                      cleared entirely. */}
                  <input
                    type="color"
                    className="cm-colour-swatch"
                    value={derived?.fill ?? effectiveAccent}
                    onChange={(e) => edit("brand_accent", e.target.value)}
                    aria-label="Pick an accent colour"
                  />
                </div>
                <p className="cm-field-hint" id={`${accentId}-hint`}>
                  One hex value, like #2563eb. Hover, pressed, link and tint variants are derived from
                  it, and text on buttons is picked by measuring contrast. Empty means the built-in
                  accent.
                </p>
                {accentUnreadable && (
                  <p className="cm-field-hint">
                    Not a hex colour yet — the preview stays on the built-in accent.
                  </p>
                )}
                {errorField === "brand_accent" && (
                  <p className="cm-field-error" role="alert">
                    {saveError}
                  </p>
                )}
              </div>

              <div className="cm-field">
                <label htmlFor={logoId}>Logo URL</label>
                <input
                  id={logoId}
                  className="cm-input cm-input-code"
                  value={draft.brand_logo_url}
                  onChange={(e) => edit("brand_logo_url", e.target.value)}
                  spellCheck={false}
                  autoCapitalize="none"
                  aria-describedby={`${logoId}-hint`}
                  aria-invalid={errorField === "brand_logo_url" || undefined}
                />
                <p className="cm-field-hint" id={`${logoId}-hint`}>
                  An https:// URL, a /path served by this deployment, or a data:image/… URI. Replaces
                  the built-in mark. Empty keeps the mark.
                </p>
                {logoFailed && (
                  <p className="cm-field-hint">That image didn&rsquo;t load. The built-in mark is shown instead.</p>
                )}
                {errorField === "brand_logo_url" && (
                  <p className="cm-field-error" role="alert">
                    {saveError}
                  </p>
                )}
              </div>
            </fieldset>

            {/* The real components, running the derived tokens. Outside the
                fieldset so it keeps showing what is being saved while the
                inputs are locked. */}
            <div className="cm-preview" style={previewStyle} role="group" aria-label="Branding preview">
              <div className="cm-preview-chrome">
                <span className="cm-sidebar-brand">
                  {previewLogo && !logoFailed ? (
                    <img
                      className="cm-brand-logo"
                      src={previewLogo}
                      alt=""
                      onError={() => setLogoFailed(true)}
                    />
                  ) : (
                    <BrandGlyph />
                  )}
                  <span className="cm-brand-name">{previewName}</span>
                </span>
                <span className="cm-badge">Preview</span>
              </div>
              <div className="cm-preview-body">
                <span className="cm-preview-nav">
                  <IconListChecks />
                  Tasks
                </span>
                <span className="cm-btn cm-btn-primary">Retry</span>
                <span className="cm-link">All tasks</span>
              </div>
              <p className="cm-preview-readout">
                {derived ? (
                  <>
                    <span>
                      Button text {derived.fillText}, {derived.fillRatio.toFixed(2)}:1
                    </span>
                    <span>
                      Link text {derived.linkText}, {derived.linkRatio.toFixed(2)}:1
                    </span>
                  </>
                ) : (
                  <span>Built-in accent, contrast checked for both themes</span>
                )}
              </p>
            </div>

            {saveError && !errorField && (
              <p className="cm-error" role="alert">
                {saveError}
              </p>
            )}

            {saved && (
              <p className="cm-notice" role="status">
                Branding saved. Everyone sees it from their next page load.
              </p>
            )}

            {canEdit && (
              <div className="cm-settings-actions">
                <button type="submit" className="cm-btn cm-btn-primary" disabled={saving || !dirty}>
                  Save branding
                </button>
                <button
                  type="button"
                  className="cm-btn cm-btn-secondary"
                  onClick={handleReset}
                  disabled={saving || isDefault}
                >
                  Reset to defaults
                </button>
                {saving && (
                  <span className="cm-loading">
                    <span className="cm-live-dot" aria-hidden="true" />
                    Saving&hellip;
                  </span>
                )}
                {!saving && !dirty && !saved && (
                  <span className="cm-field-hint">No unsaved changes.</span>
                )}
              </div>
            )}
          </form>
        )}
      </section>

      <section className="cm-card cm-settings-section" aria-labelledby={appearanceHeadingId}>
        <div className="cm-card-header">
          <h2 className="cm-card-title" id={appearanceHeadingId}>
            Appearance
          </h2>
          <p className="cm-card-description">
            <span className="cm-scope-tag">This browser only.</span>{" "}
            <span>
              Your theme is stored on this device and takes effect immediately. Colleagues, and your
              own other browsers, keep their own choice.
            </span>
          </p>
        </div>

        {/* Same control as the sidebar, same store behind it — changing it in
            either place moves the other. */}
        <div className="cm-settings-actions">
          <ThemeSwitch labelled />
          <span className="cm-field-hint">
            {themePreference === "system"
              ? `Auto follows this device, which is set to ${resolvedTheme} right now.`
              : `Fixed to ${themePreference}, whatever this device is set to.`}
          </span>
        </div>
      </section>
    </div>
  );
}
