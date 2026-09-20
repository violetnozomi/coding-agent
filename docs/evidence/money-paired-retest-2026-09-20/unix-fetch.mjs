// Transport and observation only; preserve request/response bodies in live flow.
import http from 'node:http';
globalThis.fetch = async (input, options = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url ?? input);
  if (url.origin !== 'http://localhost' || url.pathname !== '/v1/chat/completions') {
    throw new Error(`Comparison rejected endpoint: ${url.origin}${url.pathname}`);
  }
  const stack = new Error().stack ?? '';
  // Preserve real call-site attribution. Unidentified calls conservatively use main budget.
  const purpose = /compact|summari/i.test(stack) ? 'compaction' :
    /verifier|sidecar/i.test(stack) ? 'verifier' : 'coding';
  return new Promise((resolve,reject) => {
    const request=http.request({socketPath:process.env.SMOKE_SOCKET,path:url.pathname,
      method:options.method??'POST',headers:{...Object.fromEntries(new Headers(options.headers)),
        'content-length':Buffer.byteLength(options.body??''),'x-nz-capture-purpose':purpose,
        'x-nz-capture-stack':Buffer.from(stack).toString('base64')},signal:options.signal},response=>{
      const chunks=[];response.on('data',chunk=>chunks.push(chunk));response.on('error',reject);
      response.on('end',()=>resolve(new Response(Buffer.concat(chunks),{status:response.statusCode,headers:response.headers})));
    });
    request.on('error',reject);request.end(options.body);
  });
};
