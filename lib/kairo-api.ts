const ALLOWED_READ_PATHS = new Set([
  'system/status',
  'research/qualification/latest',
  'research/matrix/q1-2024',
]);

export function resolveKairoApiUrl(path: string): URL {
  if (!ALLOWED_READ_PATHS.has(path)) {
    throw new Error('Kairo API path is not exposed by the command-center proxy');
  }

  const configuredBase = process.env.KAIRO_API_BASE_URL;
  if (!configuredBase) {
    throw new Error('KAIRO_API_BASE_URL is not configured');
  }
  const base = new URL(configuredBase);
  if (base.protocol !== 'https:' && base.hostname !== 'localhost' && base.hostname !== '127.0.0.1') {
    throw new Error('KAIRO_API_BASE_URL must use HTTPS outside localhost');
  }
  return new URL(`/api/v1/${path}`, base);
}
