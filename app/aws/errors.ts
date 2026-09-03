export function errorName(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null || !("name" in error)) return undefined;
  return typeof error.name === "string" ? error.name : undefined;
}

export function errorMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "message" in error
    && typeof error.message === "string") {
    return error.message;
  }
  return String(error);
}

export function errorProperty(error: unknown, property: string): unknown {
  if (typeof error !== "object" || error === null) return undefined;
  return Reflect.get(error, property);
}
