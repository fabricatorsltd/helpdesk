<template>
  <div class="flex flex-wrap items-end gap-3 pb-3 border-b">
    <div class="flex flex-col gap-1">
      <span class="text-xs text-ink-gray-5">{{ __("Language") }}</span>
      <Select
        v-model="language"
        :options="KB_LANGUAGES"
        @update:modelValue="emit('change')"
      />
    </div>
    <div class="flex flex-col gap-1">
      <span class="text-xs text-ink-gray-5">{{ __("Visibility") }}</span>
      <Select
        v-model="visibility"
        :options="VISIBILITY_OPTIONS"
        @update:modelValue="emit('change')"
      />
    </div>
    <div
      v-if="visibility === 'Restricted'"
      class="flex flex-col gap-1 min-w-[240px]"
    >
      <span class="text-xs text-ink-gray-5">{{
        __("Visible to customers")
      }}</span>
      <div v-if="customers.length" class="flex flex-wrap gap-1.5 mb-1">
        <span
          v-for="c in customers"
          :key="c"
          class="flex items-center gap-1 rounded bg-surface-gray-3 px-2 py-0.5 text-p-sm text-ink-gray-7"
        >
          {{ c }}
          <button
            class="text-ink-gray-5 hover:text-ink-gray-8"
            :title="__('Remove')"
            @click="removeCustomer(c)"
          >
            <IconX class="h-3 w-3" />
          </button>
        </span>
      </div>
      <Autocomplete
        :placeholder="__('Add a customer...')"
        :options="availableCustomers"
        :value="null"
        @change="addCustomer"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Autocomplete } from "@/components";
import { __ } from "@/translation";
import { createResource, Select } from "frappe-ui";
import { computed } from "vue";
import IconX from "~icons/lucide/x";

// Audience and language of a knowledge base article, shared by the create and
// the edit page. Values map to the fab_language / fab_visibility / fab_customers
// fields on HD Article; an empty language means "not set".
const language = defineModel<string>("language", { default: "" });
const visibility = defineModel<string>("visibility", { default: "Public" });
const customers = defineModel<string[]>("customers", { default: () => [] });

const emit = defineEmits(["change"]);

const KB_LANGUAGES = [
  { label: __("Not set"), value: "" },
  { label: "Italiano", value: "it" },
  { label: "English", value: "en" },
  { label: "Français", value: "fr" },
  { label: "Español", value: "es" },
];
const VISIBILITY_OPTIONS = [
  { label: __("Public"), value: "Public" },
  { label: __("Restricted"), value: "Restricted" },
];

const customerOptions = createResource({
  url: "frappe.client.get_list",
  makeParams: () => ({
    doctype: "HD Customer",
    fields: ["name"],
    limit_page_length: 0,
    order_by: "name asc",
  }),
  auto: true,
  transform: (data: { name: string }[]) =>
    data.map((c) => ({ label: c.name, value: c.name })),
});
const availableCustomers = computed(() =>
  (customerOptions.data || []).filter(
    (o: { value: string }) => !customers.value.includes(o.value)
  )
);

function addCustomer(option: { value?: string } | null) {
  if (option?.value && !customers.value.includes(option.value)) {
    customers.value = [...customers.value, option.value];
    emit("change");
  }
}

function removeCustomer(name: string) {
  customers.value = customers.value.filter((c) => c !== name);
  emit("change");
}
</script>
