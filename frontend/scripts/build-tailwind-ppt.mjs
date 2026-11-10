import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const manifestPath = resolve(root, '../backend/app/tailwind-ppt-v1.json');
const profile = JSON.parse(readFileSync(manifestPath, 'utf8'));
if (profile.profile !== 'tailwind-ppt-v1' || new Set(profile.classes).size !== profile.classes.length) {
  throw new Error('Invalid Tailwind PPT profile');
}

const cliPackagePath = createRequire(import.meta.url).resolve('@tailwindcss/cli/package.json');
const cliPackage = JSON.parse(readFileSync(cliPackagePath, 'utf8'));
const cli = resolve(dirname(cliPackagePath), cliPackage.bin.tailwindcss);
const input = resolve(root, '.tailwind-ppt.build.css');
const output = resolve(root, '../backend/app/tailwind-ppt-v1.css');
const source = [
  readFileSync(resolve(root, 'tailwind-ppt.css'), 'utf8'),
  `@source inline("${profile.classes.join(' ')}");`,
].join('\n');

try {
  writeFileSync(input, source, { flag: 'w' });
  execFileSync(process.execPath, [cli, '-i', input, '-o', output, '--minify'], { stdio: 'inherit' });
} finally {
  rmSync(input, { force: true });
}
