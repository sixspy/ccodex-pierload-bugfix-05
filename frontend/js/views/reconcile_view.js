export const reconcileViewTemplate = `
<section class="grid-2">
  <section class="panel">
    <h2>提单对账</h2>
    <p class="hint">汇总过磅毛重与提单申报吨比对。</p>
    <div class="form-row">
      <div><label>提单号</label><input v-model="reconCode"/></div>
    </div>
    <div class="actions">
      <button @click="loadReconcile">对账</button>
      <button class="secondary" @click="loadAllReconcile">全量</button>
    </div>
    <div v-if="reconOne" class="detail-box">
      <p>申报 {{ reconOne.declared_gross_t }}t · 过磅合计 {{ reconOne.ticket_sum_t }}t</p>
      <p :class="reconOne.balanced ? 'ok' : 'bad'">{{ reconOne.balanced ? '账实平衡' : '偏差待查' }}</p>
      <p v-if="!reconOne.balanced && reconOne.reason" class="recon-warn">{{ reconOne.reason }}</p>
      <p>快照数 {{ reconOne.seal_count }} · 可出证明 {{ reconOne.proof_ready ? '是' : '否' }}</p>
    </div>
    <div v-if="reconcile && reconcile.orphans.length" class="detail-box">
      <p class="recon-warn">无提单票 {{ reconcile.orphan_count }} 张 · 累计在港毛重 {{ reconcile.orphan_gross_t }}t（不计入任何提单对账）</p>
      <table class="data-table compact">
        <thead><tr><th>票号</th><th>提单(无匹配)</th><th>车道</th><th>毛重</th></tr></thead>
        <tbody>
          <tr v-for="o in reconcile.orphans" :key="o.id">
            <td>{{ o.ticket_code }}</td>
            <td>{{ o.bl_ref }}</td>
            <td>{{ o.lane_code }}</td>
            <td>{{ fmtT(o.gross_t) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="reconcile && reconcile.banned_today.length" class="detail-box">
      <p class="recon-warn">当日无单禁建（已锁死）</p>
      <table class="data-table compact">
        <thead><tr><th>提单</th><th>尝试</th><th>最后判定</th></tr></thead>
        <tbody>
          <tr v-for="b in reconcile.banned_today" :key="b.target">
            <td>{{ b.target }}</td>
            <td>{{ b.attempts }}</td>
            <td>{{ b.last_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <ul v-if="reconAllItems.length">
      <li v-for="r in reconAllItems" :key="r.bl_code">{{ r.bl_code }} · {{ r.balanced ? '平' : '偏' }}</li>
    </ul>
  </section>
  <section class="panel">
    <h2>单票证明</h2>
    <div class="form-row"><div><label>票号</label><input v-model="proofCode"/></div></div>
    <div class="actions"><button @click="loadProof">拉证明</button></div>
    <div v-if="proofOne" class="detail-box">
      <p>提单匹配 {{ proofOne.manifest_matches_bl ? '是' : '否' }}</p>
      <p>超重闭环 {{ proofOne.overload_closed ? '是' : '否' }}</p>
      <p>已放行 {{ proofOne.released ? '是' : '否' }}</p>
      <p>一致 {{ proofOne.coherent ? '是' : '否' }}</p>
    </div>
  </section>
</section>
`;