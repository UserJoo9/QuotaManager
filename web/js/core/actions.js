// Every mutation goes through the SAME endpoints the classic dashboard uses.
import { api } from "./api.js";
export const actions = {
  patchUser: (id, body) => api.patch(`/api/users/${id}`, body),
  patchDevice: (id, body) => api.patch(`/api/devices/${id}`, body),
  dnsUser: (id, dns_server) => api.patch(`/api/users/${id}/dns`, { dns_server }),
  dnsDevice: (id, dns_server) => api.patch(`/api/devices/${id}/dns`, { dns_server }),
  blockUser: (id, block) => api.patch(`/api/users/${id}`, { block }),
  blockDevice: (id, block) => api.patch(`/api/devices/${id}`, { block }),
  kickUser: (id) => api.del(`/api/users/${id}?blacklist=false`),
  blacklistUser: (id) => api.del(`/api/users/${id}?blacklist=true`),
  kickDevice: (id) => api.del(`/api/devices/${id}?blacklist=false`),
  blacklistDevice: (id) => api.del(`/api/devices/${id}?blacklist=true`),
  approveGuest: (uid) => api.post(`/api/guest/user/${uid}/approve`),
  rejectGuest: (uid) => api.post(`/api/guest/user/${uid}/reject`),
  makeRegular: (uid) => api.post(`/api/users/${uid}/make-regular`),
  createUser: (body) => api.post("/api/users", body),
  createDevice: (body) => api.post("/api/devices", body),
  topupUser: (id, extra_gb) => api.post(`/api/users/${id}/topup`, { extra_gb }),
  topupDevice: (id, extra_gb) => api.post(`/api/devices/${id}/topup`, { extra_gb }),
};
