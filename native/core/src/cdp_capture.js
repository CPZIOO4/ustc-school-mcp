(async()=>{
  // Mirrors the Playwright storage-state serialized value format. Unsupported
  // browser values fail the snapshot rather than silently discarding device trust.
  const encode=(x,refs=new Map())=>{
    if(x===undefined)return {v:'undefined'};
    if(x===null)return {v:'null'};
    if(typeof x==='number' && !Number.isFinite(x))return {v:String(x)};
    if(Object.is(x,-0))return {v:'-0'};
    if(typeof x==='bigint')return {bi:String(x)};
    if(['string','number','boolean'].includes(typeof x))return x;
    if(refs.has(x))return {ref:refs.get(x)};
    if(x instanceof Date)return {d:x.toISOString()};
    if(x instanceof URL)return {u:x.href};
    if(x instanceof RegExp)return {r:{p:x.source,f:x.flags}};
    const id=refs.size+1;refs.set(x,id);
    if(Array.isArray(x))return {id,a:x.map(v=>encode(v,refs))};
    if(Object.getPrototypeOf(x)===Object.prototype || Object.getPrototypeOf(x)===null)return {id,o:Object.keys(x).map(k=>({k,v:encode(x[k],refs)}))};
    throw Error('Unsupported device storage value');
  };
  const request=r=>new Promise((resolve,reject)=>{r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(Error('Device storage capture failed'));});
  const result={origin:location.origin,localStorage:Object.keys(localStorage).map(name=>({name,value:localStorage.getItem(name)})),indexedDB:[]};
  for(const item of await indexedDB.databases()){
    const db=await request(indexedDB.open(item.name));
    try{
      const entry={name:db.name,version:db.version,stores:[]};
      if(db.objectStoreNames.length){
        const tx=db.transaction([...db.objectStoreNames],'readonly');
        // Queue requests synchronously: waiting before queueing the next store
        // lets Chrome auto-commit the transaction and would lose its contents.
        const stores=[...db.objectStoreNames].map(name=>{const s=tx.objectStore(name);return {name:s.name,keyPath:s.keyPath,autoIncrement:s.autoIncrement,indexes:[...s.indexNames].map(name=>{const i=s.index(name);return {name:i.name,keyPath:i.keyPath,unique:i.unique,multiEntry:i.multiEntry};}),keys:request(s.getAllKeys()),values:request(s.getAll())};});
        for(const {name,keyPath,autoIncrement,indexes,keys,values} of stores){
          const k=await keys,v=await values;
          const store={name,autoIncrement,indexes,records:v.map((value,n)=>({valueEncoded:encode(value),...(keyPath===null?{keyEncoded:encode(k[n])}:{})}))};
          if(keyPath!==null)store.keyPath=keyPath;
          entry.stores.push(store);
        }
      }
      result.indexedDB.push(entry);
    }finally{db.close();}
  }
  return result;
})()
