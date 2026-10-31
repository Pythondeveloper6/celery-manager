// Turns a task's argument JSON Schema (from `args_schemas`) into simple form
// fields for the Run now dialog. Only flat schemas of scalars and enums get a
// form; anything else keeps the JSON editor. The kwargs JSON text stays the
// single source of truth: each field edit rewrites one key of it, and the
// server validates the result against the same model before dispatching.

export type FieldKind = "string" | "integer" | "number" | "boolean" | "enum";

export interface SchemaField {
  name: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  description: string | null;
  options: string[];
}

type JsonSchema = Record<string, unknown>;

function scalarKind(prop: JsonSchema): FieldKind | null {
  if (Array.isArray(prop.enum) && prop.enum.every((v) => typeof v === "string")) return "enum";
  const type = prop.type;
  if (type === "string" || type === "integer" || type === "number" || type === "boolean") return type;
  // Optional[X] from Pydantic: anyOf [X, null].
  if (Array.isArray(prop.anyOf)) {
    const variants = (prop.anyOf as JsonSchema[]).filter((v) => v.type !== "null");
    if (variants.length === 1 && variants.length < prop.anyOf.length) return scalarKind(variants[0]);
  }
  return null;
}

export function schemaFields(schema: unknown): SchemaField[] | null {
  if (!schema || typeof schema !== "object") return null;
  const s = schema as JsonSchema;
  const props = s.properties;
  if (!props || typeof props !== "object") return null;
  const required = new Set(Array.isArray(s.required) ? (s.required as string[]) : []);
  const fields: SchemaField[] = [];
  for (const [name, raw] of Object.entries(props as Record<string, JsonSchema>)) {
    const kind = scalarKind(raw);
    if (kind === null) return null;
    fields.push({
      name,
      label: typeof raw.title === "string" ? raw.title : name,
      kind,
      required: required.has(name),
      description: typeof raw.description === "string" ? raw.description : null,
      options: kind === "enum" ? (raw.enum as string[]) : [],
    });
  }
  return fields.length > 0 ? fields : null;
}

function parseObject(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text.trim() === "" ? "{}" : text);
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

export function readKwarg(text: string, name: string): unknown {
  return parseObject(text)?.[name];
}

/** `text` with `name` set from a form control; an empty input removes the key.
 * Unparseable text is returned unchanged so a half-typed edit is never lost. */
export function setKwarg(text: string, name: string, value: string | boolean, kind: FieldKind): string {
  const current = parseObject(text);
  if (current === null) return text;
  const next = { ...current };
  if (value === "") {
    delete next[name];
  } else if (kind === "boolean") {
    next[name] = Boolean(value);
  } else if ((kind === "integer" || kind === "number") && typeof value === "string") {
    const n = Number(value);
    next[name] = value.trim() !== "" && Number.isFinite(n) && (kind === "number" || Number.isInteger(n)) ? n : value;
  } else {
    next[name] = value;
  }
  return JSON.stringify(next, null, 2);
}
