// utils/request.js —— 统一请求封装
const app = getApp();

function request(path, data = {}, method = 'GET') {
  return new Promise((resolve, reject) => {
    wx.request({
      url: app.globalData.baseUrl + path,
      data,
      method,
      header: { 'content-type': 'application/json' },
      success(res) {
        const body = res.data;
        if (body && body.code === 0) {
          resolve(body.data);
        } else {
          const msg = (body && body.msg) || '请求失败，请稍后重试';
          wx.showToast({ title: msg, icon: 'none', duration: 2500 });
          reject(new Error(msg));
        }
      },
      fail(err) {
        wx.showToast({ title: '网络异常，请检查后端服务', icon: 'none', duration: 2500 });
        reject(err);
      }
    });
  });
}

module.exports = {
  request,
  // 常用接口封装
  getDepartments: () => request('/api/mini/departments'),
  getDoctors: (departmentId) => request('/api/mini/doctors', { department_id: departmentId }),
  getSchedules: (doctorId, days = 7) => request('/api/mini/schedules', { doctor_id: doctorId, days }),
  getPatient: (idCard) => request('/api/mini/patient', { id_card: idCard }),
  register: (payload) => request('/api/mini/register', payload, 'POST'),
  // 退号必须同时带 id_card：服务端要用它校验「这单挂号确实属于这个身份证号」，
  // 否则任何人遍历 registration_id 就能退掉别人的号。
  cancelRegistration: (registrationId, idCard, reason) => request('/api/mini/cancel', { registration_id: registrationId, id_card: idCard, reason }, 'POST'),
  myRegistrations: (idCard) => request('/api/mini/my_registrations', { id_card: idCard }),
  search: (keyword) => request('/api/mini/search', { keyword })
};
