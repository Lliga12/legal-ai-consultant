// Deterministic audio lifecycle check, without browser, microphone or paid API.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = readFileSync(require('node:path').join(__dirname, '../ui/voice.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

async function check(cancelDuringPermission) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {disabled:false,textContent:'',children:[],
      classList:{add(){},remove(){}},append(p){this.children.push(p);},scrollIntoView(){}});
    return elements.get(id);
  };
  let processor, socket, micStopped=0, audioClosed=0, permissionResolve;
  let permissionEntered;
  const permissionRequested = new Promise(resolve=>permissionEntered=resolve);
  const events = {};
  const stream = {getTracks:()=>[{stop(){micStopped++;}}]};
  const permission = cancelDuringPermission ? new Promise(resolve=>permissionResolve=resolve) : Promise.resolve(stream);
  class Audio {
    constructor(){this.sampleRate=48000;this.currentTime=0;this.destination={};}
    resume(){return Promise.resolve();}close(){audioClosed++;return Promise.resolve();}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createScriptProcessor(){processor={connect(){},disconnect(){}};return processor;}
    createGain(){return {gain:{value:1},connect(){},disconnect(){}};}
  }
  class Socket {
    static OPEN=1;
    constructor(){socket=this;this.readyState=1;this.bufferedAmount=0;this.sent=[];}
    send(value){this.sent.push(value);}close(){this.readyState=3;this.closed=true;}
  }
  vm.runInNewContext(source, {document:{getElementById:el,createElement:()=>({textContent:'',scrollIntoView(){}}),
      addEventListener:(name,fn)=>events[name]=fn},location:{hash:'#test-ticket',pathname:'/voice/',protocol:'https:',host:'example.test'},
    history:{replaceState(){}}, navigator:{mediaDevices:{getUserMedia:()=>{permissionEntered();return permission;}}},
    AudioContext:Audio,WebSocket:Socket,addEventListener:(name,fn)=>events[name]=fn,
    setTimeout:()=>1,clearTimeout(){},ArrayBuffer,DataView,Math,JSON});
  const starting=el('start').onclick();
  await permissionRequested;
  if(cancelDuringPermission){
    el('stop').onclick();permissionResolve(stream);await starting;
    assert.equal(micStopped,1);assert.equal(audioClosed,1);assert.equal(socket,undefined);
    return;
  }
  await starting;
  socket.onopen();
  assert.equal(JSON.parse(socket.sent[0]).ticket,'test-ticket');
  socket.onmessage({data:JSON.stringify({type:'ready'})});
  processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(4096).fill(.5)}});
  assert.ok(socket.sent[1] instanceof ArrayBuffer);
  assert.equal(new DataView(socket.sent[1]).getInt16(0,true),16384);
  events.pagehide();
  assert.equal(socket.closed,true);assert.equal(micStopped,1);assert.equal(audioClosed,1);
  assert.equal(processor.onaudioprocess,null);
}
(async()=>{await check(false);await check(true);console.log('PASS: PCM resampling, socket shutdown, microphone release, late permission cancellation');})().catch(e=>{console.error(e);process.exitCode=1;});
