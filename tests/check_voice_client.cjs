// Deterministic audio lifecycle check, without browser, microphone or paid API.
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = readFileSync(require('node:path').join(__dirname, '../ui/voice.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

async function check(cancelDuringPermission, useWorklet=false) {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, {disabled:false,textContent:'',children:[],
      classList:{add(){},remove(){}},append(p){this.children.push(p);},scrollIntoView(){}});
    return elements.get(id);
  };
  let processor, socket, audio, micStopped=0, audioClosed=0, permissionResolve, resumes=0, moduleUrl;
  let permissionEntered;
  const permissionRequested = new Promise(resolve=>permissionEntered=resolve);
  const events = {};
  const timers=new Map();let timerId=0;
  const stream = {getTracks:()=>[{stop(){micStopped++;}}]};
  const permission = cancelDuringPermission ? new Promise(resolve=>permissionResolve=resolve) : Promise.resolve(stream);
  class Audio {
    constructor(){audio=this;this.state='suspended';this.sampleRate=48000;this.currentTime=0;this.destination={};
      if(useWorklet)this.audioWorklet={addModule:async url=>{moduleUrl=url;if(useWorklet==='fail')throw Error('unsupported');}};}
    resume(){resumes++;this.state='running';return Promise.resolve();}close(){audioClosed++;return Promise.resolve();}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createScriptProcessor(){processor={connect(){},disconnect(){}};return processor;}
    createGain(){return {gain:{value:1},connect(){},disconnect(){}};}
  }
  class Worklet {
    constructor(context,name){assert.equal(name,'legal-microphone');processor=this;
      this.port={onmessage:null,close(){this.closed=true;}};}
    connect(){}disconnect(){}
  }
  class Socket {
    static OPEN=1;
    constructor(){socket=this;this.readyState=1;this.bufferedAmount=0;this.sent=[];}
    send(value){this.sent.push(value);}close(){this.readyState=3;this.closed=true;}
  }
  const document={hidden:false,getElementById:el,createElement:()=>({textContent:'',scrollIntoView(){}}),
      addEventListener:(name,fn)=>events[name]=fn};
  vm.runInNewContext(source, {document,location:{hash:'#test-ticket',pathname:'/voice/',protocol:'https:',host:'example.test'},
    history:{replaceState(){}}, navigator:{mediaDevices:{getUserMedia:()=>{permissionEntered();return permission;}}},
    AudioContext:Audio,AudioWorkletNode:Worklet,WebSocket:Socket,addEventListener:(name,fn)=>events[name]=fn,
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id),ArrayBuffer,DataView,Math,JSON});
  const starting=el('start').onclick();
  await permissionRequested;
  if(cancelDuringPermission){
    el('stop').onclick();permissionResolve(stream);await starting;
    assert.equal(micStopped,1);assert.equal(audioClosed,1);assert.equal(socket,undefined);
    return;
  }
  await starting;
  assert.equal(resumes,2,'Resume again after Android microphone permission / audio-route changes');
  if(useWorklet)assert.equal(moduleUrl,'/voice/processor.js');
  socket.onopen();
  assert.equal(JSON.parse(socket.sent[0]).ticket,'test-ticket');
  socket.onmessage({data:JSON.stringify({type:'ready'})});
  const [micTimerId,micTimer]=[...timers].find(([,t])=>t.ms===10000);
  timers.delete(micTimerId);micTimer.fn();
  assert.equal(el('resume').hidden,false);
  audio.state='suspended';audio.onstatechange();
  await el('resume').onclick();
  assert.equal(audio.state,'running');assert.equal(el('resume').hidden,true);
  const capture=()=>{const input=new Float32Array(4096).fill(.5);
    if(processor.port)processor.port.onmessage({data:input});
    else processor.onaudioprocess({inputBuffer:{getChannelData:()=>input}});};
  capture();
  assert.ok(socket.sent[1] instanceof ArrayBuffer);
  assert.equal(new DataView(socket.sent[1]).getInt16(0,true),16384);
  document.hidden=true;events.visibilitychange();
  assert.ok(!socket.closed,'Brief Android overlays must not immediately end the conversation');
  capture();assert.equal(socket.sent.length,2,'No microphone upload while hidden');
  document.hidden=false;events.visibilitychange();
  assert.equal([...timers.values()].filter(t=>t.ms===10000).length,0,'Returning cancels hidden-tab timeout');
  capture();assert.equal(socket.sent.length,3);
  events.pagehide();
  assert.equal(socket.closed,true);assert.equal(micStopped,1);assert.equal(audioClosed,1);
  assert.equal(processor.onaudioprocess,null);
  if(processor.port)assert.equal(processor.port.closed,true);
}
function checkProcessor(){
  let Processor;const sent=[];
  vm.runInNewContext(readFileSync(require('node:path').join(__dirname,'../ui/voice_processor.js'),'utf8'),{
    AudioWorkletProcessor:class{constructor(){this.port={postMessage:samples=>sent.push(Array.from(samples))};}},
    registerProcessor:(name,klass)=>{assert.equal(name,'legal-microphone');Processor=klass;},Float32Array});
  const processor=new Processor();
  for(let i=0;i<32;i++)assert.equal(processor.process([[new Float32Array(128).fill(.5)]]),true);
  assert.equal(sent.length,1);assert.equal(sent[0].length,4096);assert.ok(sent[0].every(s=>s===.5));
  processor.process([]);assert.equal(sent.length,1);
}
(async()=>{await check(false);await check(true);await check(false,true);await check(false,'fail');checkProcessor();
  console.log('PASS: worklet and fallback capture, PCM resampling, Android resume, brief overlays, microphone cleanup and late permission cancellation');
})().catch(e=>{console.error(e);process.exitCode=1;});
