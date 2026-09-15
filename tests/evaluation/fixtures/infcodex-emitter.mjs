// Generate raw fixture bytes using the fixed reference's real JSON emitter.
import {pathToFileURL} from 'node:url';
const {createJsonEvents} = await import(pathToFileURL(process.argv[2]));
const events = createJsonEvents();
events.onToolUseStart({id:'call-1', name:'bash', input:{command:'python -m unittest -v'} });
events.onToolResult({id:'call-1', name:'bash', content:'Ran 1 test\nFAILED (failures=1)'}, {seq:1});
events.onIterationEnd({iter:1,maxIter:8,tokenCount:16,tokenSource:'api',usage:{inputTokens:12,outputTokens:4,totalTokens:16}});
events.onTurnCompleted({turnId:'turn-1',sessionId:'fixture',seq:2});
