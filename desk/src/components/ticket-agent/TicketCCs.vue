<template>
  <div class="mt-4 flex flex-col gap-2">
    <span class="text-xs text-ink-gray-5">{{ __("CCs") }}</span>
    <div v-if="ccs.length" class="flex flex-wrap gap-1.5">
      <span
        v-for="email in ccs"
        :key="email"
        class="flex items-center gap-1 rounded bg-surface-gray-3 px-2 py-0.5 text-p-sm text-ink-gray-7"
      >
        {{ email }}
        <button
          class="text-ink-gray-5 hover:text-ink-gray-8"
          :title="__('Remove')"
          @click="remove(email)"
        >
          <LucideX class="h-3 w-3" />
        </button>
      </span>
    </div>
    <div class="flex items-center gap-2">
      <input
        v-model="newEmail"
        type="email"
        :placeholder="__('Add email...')"
        class="flex-1 min-w-0 rounded border border-outline-gray-2 bg-surface-white px-2 py-1 text-p-sm text-ink-gray-8 focus:outline-none"
        @keyup.enter="add"
      />
      <Button :label="__('Add')" size="sm" :disabled="!isValid || loading" @click="add" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { __ } from "@/translation";
import { TicketSymbol } from "@/types";
import { Button, call } from "frappe-ui";
import { computed, inject, ref } from "vue";
import LucideX from "~icons/lucide/x";

const ticket = inject(TicketSymbol)!;
const newEmail = ref("");
const loading = ref(false);

const ccs = computed(() =>
  (ticket.value?.doc?.fab_cc || "")
    .split(",")
    .map((s: string) => s.trim().toLowerCase())
    .filter(Boolean)
);

const isValid = computed(() => /.+@.+\..+/.test(newEmail.value.trim()));

function run(method: string, email: string) {
  loading.value = true;
  return call("run_doc_method", {
    dt: "HD Ticket",
    dn: ticket.value.name,
    method,
    args: { email },
  })
    .then(() => ticket.value.reload())
    .finally(() => (loading.value = false));
}

function add() {
  if (!isValid.value) return;
  run("add_cc", newEmail.value.trim().toLowerCase()).then(() => (newEmail.value = ""));
}

function remove(email: string) {
  run("remove_cc", email);
}
</script>
