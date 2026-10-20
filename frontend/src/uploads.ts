import { reactive } from 'vue'
import { api, type DocumentItem } from './api'
export type UploadTask={key:string;organizationId:string|null;file?:File;name:string;path:string;state:'queued'|'transferring'|'processing'|'ready'|'failed'|'skipped'|'unavailable';progress:number;documentId?:string;error?:string;startedAt?:number;pollFailures?:number}
export function createUploadQueue(onChange:()=>void,onError:(message:string)=>void,limits:()=>{allowed:string[];max:number;warning:number},scope:()=>string|null=()=>null){
  const state=reactive({open:false,paused:false,tasks:[] as UploadTask[],running:0})
  let generation=0
  const finish=()=>{state.running--;onChange();pump()}
  const poll=async(task:UploadTask,epoch:number)=>{
    if(epoch!==generation)return
    try{
      const doc=await api.document(task.documentId!,task.organizationId)
      if(epoch!==generation)return
      task.pollFailures=0
      if(doc.status==='ready'||doc.status==='failed'){
        task.state=doc.status;task.error=doc.error_message||undefined
        if(doc.status==='failed')onError(`“${task.name}”上传失败：${task.error||'文档处理失败'}`)
        finish();return
      }
      if(doc.status==='deleting'||doc.status==='retired'){task.state='unavailable';task.error='文档已进入退休清理流程';finish();return}
    }catch(e:any){
      if(epoch!==generation)return
      task.error=e.message
      if(e.status===403||e.status===404){task.state='unavailable';task.error='空间或文档已不可访问，停止读取进度';finish();return}
      task.pollFailures=(task.pollFailures||0)+1
      if(task.pollFailures>=3)state.paused=true
    }
    if(Date.now()-(task.startedAt||Date.now())>limits().warning*1000){task.error='处理时间较长，可继续等待';state.paused=true}
    setTimeout(()=>{void poll(task,epoch)},state.paused?10000:5000)
  }
  const transfer=async(task:UploadTask,epoch:number)=>{
    try{
      const doc=await api.upload(task.file!,n=>{if(epoch===generation)task.progress=n},task.organizationId)
      if(epoch!==generation)return
      task.documentId=doc.id;task.file=undefined;task.state='processing';onChange();void poll(task,epoch)
    }catch(e:any){
      if(epoch!==generation)return
      task.state='failed';task.error=e.message;onError(`“${task.name}”上传失败：${task.error}`);finish()
    }
  }
  const pump=()=>{
    while(!state.paused&&state.running<2){
      const task=state.tasks.find(x=>x.state==='queued')
      if(!task||!task.file)return
      state.running++;task.state='transferring';task.startedAt=Date.now();void transfer(task,generation)
    }
  }
  return {
    state,
    add(files:FileList|null){
      if(!files)return
      const config=limits(),organizationId=scope()
      for(const file of Array.from(files)){
        const ext='.'+(file.name.split('.').pop()||'').toLowerCase()
        const invalid=!config.allowed.includes(ext)?'不支持此文件格式':file.size===0?'文件为空':file.size>config.max?'文件超过大小限制':''
        state.tasks.push({key:crypto.randomUUID(),organizationId,file:invalid?undefined:file,name:file.name,path:(file as any).webkitRelativePath||file.name,state:invalid?'failed':'queued',progress:0,error:invalid||undefined})
      }
      state.open=true;pump()
    },
    recover(items:DocumentItem[]){
      for(const doc of items.filter(x=>x.status==='processing'&&!state.tasks.some(t=>t.documentId===x.id))){
        const task:UploadTask={key:crypto.randomUUID(),organizationId:doc.organization_id??null,name:doc.original_name,path:doc.original_name,state:'processing',progress:1,documentId:doc.id,startedAt:Date.now()}
        state.tasks.push(task);state.running++;void poll(task,generation)
      }
    },
    stop(){state.paused=true;state.tasks.filter(x=>x.state==='queued').forEach(x=>{x.state='skipped';x.file=undefined})},
    resume(){state.paused=false;state.tasks.forEach(x=>x.pollFailures=0);pump()},
    reset(){generation++;state.open=false;state.paused=false;state.running=0;state.tasks=[]},
  }
}
