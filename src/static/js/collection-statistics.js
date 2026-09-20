(() => {
  const source = document.getElementById('collection-chart-data');
  if (!source || typeof Chart === 'undefined') return;
  const data = JSON.parse(source.textContent);
  const muted = '#98a5ba', grid = '#3a414d', purple = '#a99cff';
  Chart.defaults.color = muted;
  Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
  Chart.defaults.font.size = 10;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const base = {responsive: true, maintainAspectRatio: false, animation: reduced ? false : {duration: 450}, plugins: {legend: {position:'bottom', labels:{usePointStyle:true, boxWidth:7, boxHeight:7, padding:13}}, tooltip:{backgroundColor:'#171b24', titleColor:'#eef1fa', bodyColor:'#c9d1e1', padding:12, cornerRadius:7}}, scales:{x:{grid:{display:false}, border:{display:false}}, y:{beginAtZero:true, grid:{color:grid}, border:{display:false}}}};
  function draw(id, type, chartData, overrides={}) {
    const canvas = document.getElementById(id);
    if (!canvas) return;
    const prior = Chart.getChart(canvas); if (prior) prior.destroy();
    if (!chartData.datasets.some(ds => ds.data.some(n => n>0))) {
      const message=document.createElement('p'); message.className='obs-empty-chart';
      message.textContent=id==='recordedTime'||id==='unitProgress' ? 'No dated progress in this period yet. Imported lifetime totals are included above.' : 'No data for this chart yet. Add to this collection or refresh catalogue details.';
      canvas.replaceWith(message); return;
    }
    new Chart(canvas,{type,data:chartData,options:{...base,...overrides}});
  }
  const bars=(labels,values,color=purple,label='Titles')=>({labels,datasets:[{label,data:values,backgroundColor:color,borderRadius:4,maxBarThickness:24}]});
  if(data.watchtime) draw('watchtimeByTitle','bar',bars(data.watchtime.labels,data.watchtime.values.map(n=>n/60),'#75bcb8','Watch time'),{indexAxis:'y',plugins:{...base.plugins,legend:{display:false},tooltip:{...base.plugins.tooltip,callbacks:{label:ctx=>{const minutes=Math.round(ctx.parsed.x*60);return `${Math.floor(minutes/60)}h ${String(minutes%60).padStart(2,'0')}m`;}}}},scales:{x:{beginAtZero:true,grid:{color:grid},title:{display:true,text:'Hours'}},y:{grid:{display:false}}}});
  draw('collectionMix','doughnut',{labels:data.mix.labels,datasets:[{data:data.mix.values,backgroundColor:data.mix.colors,borderWidth:0,hoverOffset:6}]},{scales:{},cutout:'76%'});
  draw('collectionStatus','bar',data.status,{indexAxis:'y',scales:{x:{stacked:true,grid:{color:grid},ticks:{precision:0}},y:{stacked:true,grid:{display:false}}}});
  draw('collectionTime','bar',bars(data.time.labels,data.time.values,data.time.colors,'Hours'),{indexAxis:'y',plugins:{...base.plugins,legend:{display:false}},scales:{x:{beginAtZero:true,grid:{color:grid},title:{display:true,text:'Hours'}},y:{grid:{display:false}}}});
  draw('collectionGrowth','bar',{labels:data.months,datasets:data.growth},{scales:{x:{stacked:true,grid:{display:false},ticks:{maxTicksLimit:12}},y:{stacked:true,beginAtZero:true,grid:{color:grid},ticks:{precision:0}}}});
  draw('recordedTime','line',{labels:data.months,datasets:data.recordedTime.map(d=>({...d,pointRadius:2,tension:.3,borderWidth:2}))},{interaction:{mode:'index',intersect:false},scales:{x:{grid:{display:false},ticks:{maxTicksLimit:12}},y:{beginAtZero:true,grid:{color:grid},title:{display:true,text:'Hours recorded'}}}});
  if(data.progress) draw('unitProgress','bar',bars(data.progress.labels,data.progress.values.map(n=>data.progress.unit==='hours played'?n/60:n),'#75cdb2',data.progress.unit),{plugins:{...base.plugins,legend:{display:false}},scales:{x:{grid:{display:false},ticks:{maxTicksLimit:12}},y:{beginAtZero:true,grid:{color:grid},title:{display:true,text:data.progress.unit}}}});
  for(const [id,values,horizontal] of [['collectionInvestment',data.investment,false],['collectionPlatforms',data.platforms,true],['collectionDepth',data.depth,false],['collectionGenres',data.genres,true],['collectionEras',data.eras,false],['collectionSources',data.sources,true],['collectionRatings',data.ratings,false]]){
    draw(id,'bar',bars(Object.keys(values),Object.values(values),id==='collectionGenres'?'#b89ae0':id==='collectionRatings'?'#d7b574':'#75bcb8'),{indexAxis:horizontal?'y':'x',plugins:{...base.plugins,legend:{display:false}},scales:{x:{grid:{display:horizontal,color:grid},ticks:{precision:0}},y:{beginAtZero:true,grid:{display:!horizontal,color:grid},ticks:{precision:0}}}});
  }
})();
