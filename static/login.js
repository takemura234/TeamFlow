const form=document.querySelector('#loginForm');
const error=document.querySelector('#loginError');

fetch('/api/session').then(response=>{if(response.ok)location.href='/'});

form.addEventListener('submit',async event=>{
  event.preventDefault();
  error.textContent='';
  const button=form.querySelector('button');
  button.disabled=true;
  try{
    const values=Object.fromEntries(new FormData(form));
    const response=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(data.error||'ログインできませんでした');
    location.href='/';
  }catch(reason){
    error.textContent=reason.message;
    button.disabled=false;
  }
});
