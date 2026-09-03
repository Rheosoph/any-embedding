export type Environment = Readonly<Record<string, string | undefined>>;

interface NumberEnvironmentOptions {
  readonly integer?: boolean;
  readonly minimum?: number;
  readonly maximum?: number;
}

export function requiredEnvironment(
  name: string,
  environment: Environment = process.env,
): string {
  const value = environment[name]?.trim();
  if (!value) throw new Error(`Required environment variable ${name} is not configured`);
  return value;
}

export function numberEnvironment(
  name: string,
  fallback: number,
  options: NumberEnvironmentOptions = {},
  environment: Environment = process.env,
): number {
  const raw = environment[name];
  const value = raw === undefined || raw.trim() === "" ? fallback : Number(raw);
  const minimum = options.minimum ?? Number.NEGATIVE_INFINITY;
  const maximum = options.maximum ?? Number.POSITIVE_INFINITY;

  if (!Number.isFinite(value)
    || (options.integer === true && !Number.isInteger(value))
    || value < minimum
    || value > maximum) {
    const range = `${Number.isFinite(minimum) ? minimum : "-Infinity"}`
      + `..${Number.isFinite(maximum) ? maximum : "Infinity"}`;
    throw new Error(
      `Environment variable ${name} must be ${options.integer === true ? "an integer" : "a number"}`
      + ` in range ${range}; received ${JSON.stringify(raw)}`,
    );
  }
  return value;
}

export function sha256Environment(
  name: string,
  environment: Environment = process.env,
): string {
  const value = requiredEnvironment(name, environment).toLowerCase();
  if (!/^[0-9a-f]{64}$/u.test(value)) {
    throw new Error(`Environment variable ${name} must be a 64-character SHA-256 digest`);
  }
  return value;
}
