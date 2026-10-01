// Read the canonical YAML with the parser bundled in the locked Prism tree.
// This is test tooling; the product frontend never loads backend code or YAML.
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const prismRequire = createRequire(require.resolve('@stoplight/prism-cli/package.json'));
const { parse } = prismRequire('@stoplight/yaml');
const source = path.resolve(__dirname, '../../docs/api/openapi.yaml');
process.stdout.write(JSON.stringify(parse(fs.readFileSync(source, 'utf8'))));
