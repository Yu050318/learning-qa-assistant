export type StreamEvent = {run_id:string;sequence:number;[key:string]:any}
export type StreamCursor = {runId?:string;sequence:number;failed?:boolean}

export async function readEventStream(response:Response,onEvent:(event:string,data:StreamEvent)=>void,cursor:StreamCursor):Promise<StreamEvent>{
  if(!response.body)throw new Error('浏览器无法读取流式响应')
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer=''
  const consume=(block:string)=>{
    let event='message';const data:string[]=[]
    for(const line of block.split('\n')){if(line.startsWith('event:'))event=line.slice(6).trim();else if(line.startsWith('data:'))data.push(line.slice(5).trimStart())}
    if(!data.length)return
    const value=JSON.parse(data.join('\n')) as StreamEvent
    if(!Number.isInteger(value.sequence)||value.sequence<1||!value.run_id||(cursor.runId&&value.run_id!==cursor.runId))throw new Error('流式事件标识无效')
    if(value.sequence<=cursor.sequence)return
    if(value.sequence!==cursor.sequence+1)throw new Error('流式事件序号不连续')
    onEvent(event,value);cursor.runId=value.run_id;cursor.sequence=value.sequence
    if(event==='error'){cursor.failed=true;throw Object.assign(new Error(value.message||'问答执行失败'),{code:value.code,status:value.status})}
    return event==='done'||event==='run_cancelled'||event==='run_waiting'&&value.is_current!==false?value:undefined
  }
  try{
    while(true){
      const result=await reader.read();buffer=(buffer+decoder.decode(result.value||new Uint8Array(),{stream:!result.done})).replace(/\r\n/g,'\n')
      let boundary
      while((boundary=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);const done=consume(block);if(done)return done}
      if(result.done){if(buffer.trim()){const done=consume(buffer);if(done)return done}throw new Error('流式连接中断，正在尝试恢复')}
    }
  }finally{await reader.cancel().catch(()=>{})}
}
