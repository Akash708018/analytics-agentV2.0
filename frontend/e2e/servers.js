// Global setup: the real backend (scripted model, empty state dir) and Streamlit, on free
// ports, for the whole run. Returns the teardown, which stops both and removes the workspaces
// this run made (E2E_KEEP=1 keeps them and the logs). Set E2E_APP and E2E_API to use servers
// that are already running instead. See docs/steps/F11.md.
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

const ROOT = path.resolve(__dirname, '..', '..');
const PYTHON = process.env.E2E_PYTHON || path.join(ROOT, 'frontend', '.venv', 'bin', 'python');
// The engine keeps each workspace under backend/workspace/ (backend/engine/config.py, not
// configurable; gitignored). The run's own are those its session store names.
const WORKSPACES = path.join(ROOT, 'backend', 'workspace');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function start(name, command, args, env, dir) {
  const log = path.join(dir, `${name}.log`);
  const out = fs.openSync(log, 'a');
  const child = spawn(command, args, { cwd: ROOT, env: { ...process.env, ...env },
                                       stdio: ['ignore', out, out], detached: true });
  child.on('exit', (code) => { child.exited = code ?? 'signal'; });
  return { name, child, log };
}

async function ready(server, url, seconds) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    if (server.child.exited !== undefined) break;
    try {
      if ((await fetch(url)).ok) return;
    } catch { /* still starting */ }
    await new Promise((r) => setTimeout(r, 300));
  }
  const tail = fs.readFileSync(server.log, 'utf8').split('\n').slice(-40).join('\n');
  throw new Error(`${server.name} did not start (${url}). Last log lines:\n${tail}`);
}

async function stop(server) {
  const signal = (name) => { try { process.kill(-server.child.pid, name); } catch { /* gone */ } };
  if (server.child.exited !== undefined) return;
  signal('SIGTERM');
  for (let i = 0; i < 50 && server.child.exited === undefined; i += 1) {
    await new Promise((r) => setTimeout(r, 200));
  }
  if (server.child.exited === undefined) signal('SIGKILL');
}

function workspacesMade(state) {
  const db = path.join(state, 'sessions.db');
  if (!fs.existsSync(db)) return [];
  const read = spawnSync(PYTHON, ['-c', [
    'import sqlite3, sys',
    'c = sqlite3.connect(sys.argv[1])',
    "print('\\n'.join(sorted({r[0] for t in ('sessions', 'datasets') "
      + "for r in c.execute(f'SELECT workspace_id FROM {t}')})))",
  ].join('\n'), db], { encoding: 'utf8' });
  return read.stdout.split('\n').filter((w) => /^ws_[A-Za-z0-9_-]{1,64}$/.test(w));
}

module.exports = async () => {
  if (process.env.E2E_APP && process.env.E2E_API) return undefined;
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'aa-e2e-'));
  const state = path.join(dir, 'state');
  const [apiPort, appPort] = [await freePort(), await freePort()];
  const api = `http://127.0.0.1:${apiPort}`;
  const app = `http://127.0.0.1:${appPort}`;
  const servers = [];
  try {
    const backend = start('backend', 'uv', ['run', 'uvicorn', '--factory', 'scripted_backend:make',
      '--host', '127.0.0.1', '--port', String(apiPort)],
      { AA_STATE_DIR: state, PYTHONPATH: [ROOT, __dirname].join(path.delimiter) }, dir);
    servers.push(backend);
    const streamlit = start('streamlit', PYTHON, ['-m', 'streamlit', 'run', 'frontend/app.py',
      '--server.port', String(appPort), '--server.headless', 'true',
      '--browser.gatherUsageStats', 'false'], { ANALYTICS_API_URL: api }, dir);
    servers.push(streamlit);
    await ready(backend, `${api}/health`, 120);
    await ready(streamlit, `${app}/_stcore/health`, 120);
  } catch (error) {
    await Promise.all(servers.map(stop));
    throw error;
  }
  process.env.E2E_API = api;
  process.env.E2E_APP = app;
  process.env.E2E_DIR = dir;
  console.log(`e2e: backend ${api}, app ${app}, logs ${dir}`);
  return async () => {
    await Promise.all(servers.map(stop));
    if (process.env.E2E_KEEP) return;
    for (const ws of workspacesMade(state)) {
      fs.rmSync(path.join(WORKSPACES, ws), { recursive: true, force: true });
    }
    fs.rmSync(dir, { recursive: true, force: true });
  };
};
