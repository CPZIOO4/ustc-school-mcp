// Runs in an isolated, locally fulfilled origin before any school page loads.
function decode(x, refs = new Map()) {
  if (x === undefined || x === null || typeof x !== 'object') return x;
  if ('ref' in x) { if (!refs.has(x.ref)) throw Error(); return refs.get(x.ref); }
  if ('v' in x) { const values = {undefined:undefined, null:null, NaN:NaN, Infinity:Infinity, '-Infinity':-Infinity, '-0':-0}; if (!(x.v in values)) throw Error(); return values[x.v]; }
  if ('d' in x) return new Date(x.d);
  if ('u' in x) return new URL(x.u);
  if ('bi' in x) return BigInt(x.bi);
  if ('r' in x) return new RegExp(x.r.p, x.r.f);
  if ('a' in x) { const result=[]; refs.set(x.id,result); for(const value of x.a) result.push(decode(value,refs)); return result; }
  if ('o' in x) { const result={}; refs.set(x.id,result); for(const pair of x.o) Object.defineProperty(result,pair.k,{value:decode(pair.v,refs),writable:true,enumerable:true,configurable:true}); return result; }
  throw Error('Unsupported encoded login state');
}
const request = r => new Promise((resolve,reject)=>{r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(Error('Login storage restore failed'));r.onblocked=()=>reject(Error('Login storage blocked'));});
localStorage.clear();
for(const row of s.localStorage||[]) { if(typeof row.name!=='string'||typeof row.value!=='string')throw Error(); localStorage.setItem(row.name,row.value); }
for(const spec of s.indexedDB||[]) {
  const opening=indexedDB.open(spec.name,spec.version);
  opening.onupgradeneeded=()=>{const db=opening.result;for(const store of spec.stores){const options={autoIncrement:store.autoIncrement};const key=store.keyPathArray??store.keyPath;if(key!==undefined)options.keyPath=key;const obj=db.createObjectStore(store.name,options);for(const idx of store.indexes||[])obj.createIndex(idx.name,idx.keyPathArray??idx.keyPath,{unique:idx.unique,multiEntry:idx.multiEntry});}};
  const db=await request(opening);
  try {
    if(!spec.stores.length)continue;
    const tx=db.transaction(spec.stores.map(x=>x.name),'readwrite');
    const done=new Promise((resolve,reject)=>{tx.oncomplete=resolve;tx.onerror=()=>reject(Error('Login storage restore failed'));tx.onabort=()=>reject(Error('Login storage restore aborted'));});
    for(const store of spec.stores)for(const row of store.records){const value='value' in row?row.value:decode(row.valueEncoded);const key='key' in row?row.key:decode(row.keyEncoded);tx.objectStore(store.name).add(value,key);}
    await done;
  } finally {db.close();}
}
