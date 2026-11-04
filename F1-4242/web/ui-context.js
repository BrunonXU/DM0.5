// UI-only session memory. Never stores credentials, leases or motion tickets.
export function readContext(key, fallback){try{return JSON.parse(sessionStorage.getItem('f1-uiux:'+key))??fallback;}catch{return fallback;}}
export function writeContext(key,value){try{sessionStorage.setItem('f1-uiux:'+key,JSON.stringify(value));}catch{}}
export function stateFresh(data,received,online,ageKey='status_age_s',fallback=4){const age=data?.[ageKey],ttl=data?.status_ttl_s??fallback;return !!online&&Number.isFinite(age)&&age>=0&&Number.isFinite(ttl)&&ttl>0&&Number.isFinite(received)&&age+(performance.now()-received)/1000<=ttl;}
