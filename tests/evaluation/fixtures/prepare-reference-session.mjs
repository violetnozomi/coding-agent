// Official configuration API only; no run/tool/Provider replacement.
import {pathToFileURL} from 'node:url';
const {createKodaXRuntime} = await import(pathToFileURL(process.argv[2]));
const runtime = await createKodaXRuntime({mode: 'embedded', homeDir: process.env.HOME,
  profile: 'default', defaultProvider: 'local-smoke', defaultModel: 'local-smoke'});
try {
  await runtime.sessions.create({sessionId: 'offline-F', projectPath: process.cwd(),
    gitRoot: process.cwd(), surface: 'cli', title: 'Offline F smoke'});
  const settings = await runtime.sessions.updateSettings('offline-F', {
    permissionMode: 'auto-in-project', autoModeEngine: 'rules', agentMode: 'sa',
    executionCwd: process.cwd(),
  });
  console.log(JSON.stringify(settings));
} finally {
  await runtime.close();
}
