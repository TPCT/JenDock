'use strict';
let activeTerminal = null;
function closeTerminal() {
  const t=activeTerminal;if(!t)return;activeTerminal=null;
  clearTimeout(t.timer);t.observer?.disconnect();t.term?.dispose();
  if(t.id)api('action',{action:'terminal-close',project:t.project,container:t.name,terminal_id:t.id}).catch(()=>{});
}
function renderTerminal(body,context) {
  closeTerminal();
  const intro=el('div','','terminal-intro');
  intro.append(el('h2','Container terminal'),el('p','Interactive shell inside this container, using its configured user. Sessions close after two minutes without a connection.','hint'));
  const actions=el('div','','actions'),status=el('span','Disconnected','terminal-status'),host=el('div','','terminal-host');
  host.setAttribute('aria-label','Interactive container terminal');
  const connect=button('Connect terminal',start,'primary',context.detail.state!=='running');
  const disconnect=button('Disconnect',()=>{closeTerminal();status.textContent='Disconnected';connect.disabled=false;disconnect.disabled=true;},'secondary',true);
  actions.append(connect,disconnect,status);intro.append(actions);body.append(intro,host);
  if(context.detail.state!=='running')body.append(el('p','This container is stopped. Start it from Details & version to connect.','notice'));
  const keys=el('div','','terminal-keys');
  for(const [label,value] of [['Esc','\x1b'],['Tab','\t'],['Ctrl+C','\x03'],['Ctrl+D','\x04'],['↑','\x1b[A'],['↓','\x1b[B'],['Enter','\r']])keys.append(button(label,()=>{if(activeTerminal){activeTerminal.input+=value;activeTerminal.term.focus();}},'secondary'));
  body.append(keys);
  async function start(){
    connect.disabled=true;status.textContent='Connecting…';host.replaceChildren();
    const t={project:context.project,name:context.name,input:'',cursor:0,sequence:0,term:null,id:null};activeTerminal=t;
    try {
      t.term=new Terminal({cursorBlink:true,fontSize:14,fontFamily:'ui-monospace, SFMono-Regular, Consolas, monospace',scrollback:1500,theme:{background:'#14232c',foreground:'#ecf2f5',cursor:'#74ddbb',selectionBackground:'#3b5766'},allowProposedApi:false});
      const fit=new FitAddon.FitAddon();t.term.loadAddon(fit);t.term.open(host);fit.fit();
      t.observer=new ResizeObserver(()=>{if(activeTerminal===t&&host.isConnected)fit.fit();});t.observer.observe(host);
      const result=await api('action',{action:'terminal-open',project:t.project,container:t.name,container_id:context.detail.container_id,request_id:crypto.randomUUID(),cols:t.term.cols,rows:t.term.rows});
      t.id=result.terminal_id;
      if(activeTerminal!==t){api('action',{action:'terminal-close',project:t.project,container:t.name,terminal_id:t.id}).catch(()=>{});return;}
      t.term.onData(value=>{if(t.input.length<8000)t.input+=value;});
      status.textContent='Connected · /bin/sh';disconnect.disabled=false;t.term.focus();poll();
    }catch(e){status.textContent=e.message;closeTerminal();connect.disabled=false;}
    async function poll(){
      if(activeTerminal!==t||!host.isConnected){if(activeTerminal===t)closeTerminal();return;}
      const input=t.input.slice(0,1000);t.input=t.input.slice(input.length);
      if(input)t.sequence++;
      try{
        const result=await api('action',{action:'terminal-poll',project:t.project,container:t.name,terminal_id:t.id,cursor:t.cursor,sequence:t.sequence,input,cols:t.term.cols,rows:t.term.rows});
        if(activeTerminal!==t)return;
        if(result.truncated)t.term.write('\r\n[Earlier output expired]\r\n');
        t.term.write(Uint8Array.from(atob(result.output),c=>c.charCodeAt(0)));t.cursor=result.cursor;
        if(result.closed){status.textContent='Session ended';closeTerminal();connect.disabled=false;disconnect.disabled=true;return;}
        t.timer=setTimeout(poll,180);
      }catch(e){if(activeTerminal===t){status.textContent='Disconnected. Input was not retried: '+e.message;closeTerminal();connect.disabled=false;disconnect.disabled=true;}}
    }
  }
}
window.addEventListener('pagehide',closeTerminal);
