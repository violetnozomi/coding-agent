// Official configuration API only; no run/tool/Provider replacement.
import {pathToFileURL} from 'node:url';
const {createKodaXRuntime} = await import(pathToFileURL(process.argv[2]));
const runtime = await createKodaXRuntime({mode: 'embedded', homeDir: process.env.HOME,
  profile: 'default', defaultProvider: 'local-paid', defaultModel: 'local-paid'});
try {
  await runtime.sessions.create({sessionId: 'complex-pair', projectPath: process.cwd(),
    gitRoot: process.cwd(), surface: 'cli', title: 'Complex paired task'});
  const settings = await runtime.sessions.updateSettings('complex-pair', {
    permissionMode: 'auto-in-project', autoModeEngine: 'rules', agentMode: 'sa',
    executionCwd: process.cwd(),
  });
  console.log(JSON.stringify(settings));
} finally {
  await runtime.close();
}
