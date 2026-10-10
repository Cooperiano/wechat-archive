'use strict';
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const code = fs.readFileSync(path.join(__dirname, '../wechat_archive/web/app.js'), 'utf8');
class Element {
  constructor(id='') {
    this.id=id; this.value=''; this.children=[]; this.textContent='';
    this.hidden=id==='error'; this.disabled=false; this.classList={toggle(){}};
  }
  append(...values){this.children.push(...values);}
  replaceChildren(...values){this.children=values;}
  click(){if(!this.disabled&&this.onclick)this.onclick();}
}
function fixture({hash='#synthetic-token',storage=new Map()}={}) {
  const elements=new Map(),requests=[],location={hash,pathname:'/'};
  const get=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
  vm.runInNewContext(code,{
    document:{getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>get('rooms').children},
    location,history:{replaceState(){location.hash='';}},
    sessionStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)},
    URLSearchParams,Number,Date,clearTimeout,setTimeout,
    fetch(url,options){return new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));}
  });
  return {get,requests,location,storage};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const succeed=(request,data)=>request.resolve({ok:true,json:async()=>data});
const rejectResponse=request=>request.resolve({ok:false,status:500});
const row=content=>({create_time:1791586800,content,sender_name:'Synthetic',local_type:1});
async function ready(){
  const f=fixture();succeed(f.requests[0],{conversations:2,archived_messages:201});
  succeed(f.requests[1],{rows:[{room_id:'a',name:'A',source_rows:201},{room_id:'b',name:'B',source_rows:1}],page:0,total:2});
  await flush();return f;
}
test('refresh reuses tab token; new hash replaces it',()=>{
  const f=fixture();assert.equal(f.location.hash,'');
  const reloaded=fixture({hash:'',storage:f.storage});assert.equal(reloaded.requests[0].options.headers.Authorization,'Bearer synthetic-token');
  const replaced=fixture({hash:'#replacement-token',storage:f.storage});assert.equal(replaced.requests[0].options.headers.Authorization,'Bearer replacement-token');
  assert.equal(f.storage.get('archive-token'),'replacement-token');
});
test('failed room change clears previous messages before fetching',async()=>{
  const f=await ready();f.get('rooms').children[0].click();succeed(f.requests[2],{rows:[row('ROOM A')],page:0,total:201});await flush();
  f.get('rooms').children[1].click();assert.equal(f.get('messages').children.length,0);assert(f.get('prev').disabled&&f.get('next').disabled);
  rejectResponse(f.requests[3]);await flush();assert.equal(f.get('title').textContent,'B');assert.equal(f.get('messages').children.length,0);assert.equal(f.get('error').hidden,false);
});
test('older message success or failure cannot overwrite new results',async()=>{
  for(const oldFails of [true,false]){
    const f=await ready();f.get('rooms').children[0].click();f.get('rooms').children[1].click();
    succeed(f.requests[3],{rows:[row('ROOM B')],page:0,total:1});await flush();
    if(oldFails)rejectResponse(f.requests[2]);else succeed(f.requests[2],{rows:[row('ROOM A')],page:0,total:201});
    await flush();assert.equal(f.get('messages').children[0].children[1].textContent,'ROOM B');assert.equal(f.get('error').hidden,true);
  }
});
test('older rooms failure cannot override newer rooms success',async()=>{
  const f=await ready();f.get('kind').value='private';f.get('kind').onchange();f.get('kind').value='group';f.get('kind').onchange();
  succeed(f.requests[3],{rows:[{room_id:'c',name:'GROUP',source_rows:1}],page:0,total:1});await flush();rejectResponse(f.requests[2]);await flush();
  assert.equal(f.get('rooms').children[0].textContent,'GROUP · 1');assert.equal(f.get('error').hidden,true);
});
test('pending pagination ignores repeated clicks and uses server page',async()=>{
  const f=await ready();f.get('rooms').children[0].click();succeed(f.requests[2],{rows:[row('PAGE 0')],page:0,total:201});await flush();
  f.get('next').click();const count=f.requests.length;f.get('next').click();f.get('next').click();assert.equal(f.requests.length,count);
  succeed(f.requests[3],{rows:[row('LAST PAGE')],page:2,total:201});await flush();assert.equal(f.get('page').textContent,'201 条 · 3/3 页');assert(f.get('next').disabled);
  f.get('prev').click();assert.equal(new URL(f.requests[4].url,'http://synthetic').searchParams.get('page'),'1');
  f.get('kind').onchange();assert(f.get('rooms-prev').disabled&&f.get('rooms-next').disabled);
});
test('invalid times render unknown; HTML content stays literal',async()=>{
  const f=await ready();f.get('rooms').children[0].click();
  succeed(f.requests[2],{rows:['',' ',true,false,null,1e30].map(create_time=>({...row('<img onerror=alert(1)>'),create_time})),page:0,total:6});await flush();
  for(const article of f.get('messages').children){assert(article.children[0].textContent.includes('时间未知'));assert.equal(article.children[1].textContent,'<img onerror=alert(1)>');}
});
