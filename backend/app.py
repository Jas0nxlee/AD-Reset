from flask import Flask, request, jsonify
from flask_cors import CORS
import ldap3
import winrm
import secrets
import smtplib
from email.mime.text import MIMEText
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

# 配置日志记录
def setup_logger():
    """配置日志记录器"""
    # 创建logs目录（如果不存在）
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    # 创建日志记录器
    logger = logging.getLogger('password_reset')
    logger.setLevel(logging.INFO)
    
    # 创建文件处理器（每个文件最大10MB，保留5个备份文件）
    file_handler = RotatingFileHandler(
        'logs/password_reset.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    
    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    
    # 设置日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # 添加处理器到日志记录器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# 创建Flask应用和日志记录器
app = Flask(__name__)
CORS(app)
logger = setup_logger()

# 存储验证码和过期时间
verification_codes = {}

# 从.env文件加载配置
from dotenv import load_dotenv
load_dotenv()

# LDAP配置
LDAP_SERVER = os.getenv('LDAP_SERVER')
LDAP_PORT = int(os.getenv('LDAP_PORT', 389))
# 处理LDAP基础DN
raw_base_dn = os.getenv('LDAP_BASE_DN')
if not raw_base_dn:
    LDAP_BASE_DN = ''
elif 'DC=' in raw_base_dn:
    LDAP_BASE_DN = raw_base_dn
else:
    LDAP_BASE_DN = ','.join([f'DC={x}' for x in raw_base_dn.split('.')])
LDAP_USER_DN = os.getenv('LDAP_USER_DN')
LDAP_PASSWORD = os.getenv('LDAP_PASSWORD')

# 邮件服务器配置
SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME = os.getenv('SMTP_USERNAME')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')

def get_user_email_from_ad(username):
    """从Active Directory获取用户的邮箱地址"""
    try:
        logger.info(f"开始连接LDAP服务器: {LDAP_SERVER}:{LDAP_PORT}")
        server = ldap3.Server(LDAP_SERVER, port=LDAP_PORT, use_ssl=False, get_info=ldap3.ALL)
        # 使用domain\username格式进行管理员认证
        domain = LDAP_BASE_DN.split(',')[0].replace('DC=', '')
        admin_username = LDAP_USER_DN.split(',')[0].replace('CN=', '')
        admin_dn = f"{domain}\\{admin_username}"
        logger.info(f"使用管理员DN连接LDAP: {admin_dn}")
        
        conn = ldap3.Connection(
            server,
            user=admin_dn,
            password=LDAP_PASSWORD,
            authentication=ldap3.SIMPLE,
            auto_bind=False
        )
        
        # 手动绑定并检查结果
        if not conn.bind():
            logger.error(f"LDAP绑定失败: {conn.result}")
            return None
        
        logger.info("尝试绑定LDAP连接...")
        if not conn.bind():
            logger.error(f"LDAP绑定失败: {conn.result}")
            logger.error(f"错误详情: {conn.last_error}")
            return None
        
        # 搜索目标用户
        search_filter = f'(&(objectClass=user)(sAMAccountName={username}))'
        logger.info(f"搜索用户 {username}，过滤条件: {search_filter}")
        
        # 确保基础DN格式正确
        base_dn = LDAP_BASE_DN
        logger.info(f"搜索基础DN: {base_dn}")
        
        success = conn.search(
            search_base=base_dn,
            search_filter=search_filter,
            attributes=['mail', 'userPrincipalName']
        )
        
        if not success:
            logger.error(f"LDAP搜索失败: {conn.result}")
            return None
            
        if len(conn.entries) == 0:
            logger.warning(f"未找到用户: {username}")
            logger.info(f"搜索结果为空: {conn.result}")
            return None
            
        logger.info(f"搜索结果: {conn.entries}")
            
        # 优先使用mail属性，如果不存在则使用userPrincipalName
        if hasattr(conn.entries[0], 'mail') and conn.entries[0].mail:
            user_email = conn.entries[0].mail.value
        elif hasattr(conn.entries[0], 'userPrincipalName') and conn.entries[0].userPrincipalName:
            user_email = conn.entries[0].userPrincipalName.value
        else:
            logger.error(f"用户 {username} 没有邮箱地址")
            return None
            
        logger.info(f"成功获取用户 {username} 的邮箱地址: {user_email}")
        return user_email
        
    except Exception as e:
        logger.error(f"获取用户邮箱地址时出错: {str(e)}")
        return None

def send_verification_code(email):
    """发送验证码到用户邮箱"""
    logger.info(f"开始为邮箱 {email} 发送验证码")
    code = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
    expiration_time = datetime.now() + timedelta(minutes=5)
    
    msg = MIMEText(f'您的密码重置验证码是：{code}，有效期5分钟。', 'plain', 'utf-8')
    msg['Subject'] = '密码重置验证码'
    msg['From'] = f'密码重置服务 <{SMTP_USERNAME}>'
    msg['To'] = f'{email}'
    msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S %z')

    try:
        logger.info(f"正在连接SMTP服务器: {SMTP_SERVER}:{SMTP_PORT}")
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=10)
        try:
            logger.info(f"已连接到SMTP服务器: {SMTP_SERVER}:{SMTP_PORT}")
            logger.info(f"正在登录SMTP服务器: {SMTP_USERNAME}")
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            logger.info("SMTP登录成功")
            
            logger.info(f"正在发送邮件到: {email}")
            server.send_message(msg)
            
            # 记录邮件发送详细信息
            logger.info(f"邮件发送成功: 收件人={email}, 主题=密码重置验证码, 验证码={code}")
            
            # 只有在发送成功后才存储验证码
            verification_codes[email] = {'code': code, 'expiration': expiration_time}
            return True
        finally:
            server.quit()
            
    except smtplib.SMTPException as e:
        logger.error(f"SMTP错误: {str(e)}")
        logger.error(f"SMTP错误详情: {e.smtp_error.decode() if hasattr(e, 'smtp_error') else '无详细错误信息'}")
        logger.error(f"SMTP错误代码: {e.smtp_code if hasattr(e, 'smtp_code') else '无错误代码'}")
        logger.error(f"邮件发送失败: 收件人={email}, 错误详情={str(e)}")
        return False
    except Exception as e:
        error_msg = f"发送验证码到 {email} 失败"
        logger.error(error_msg)
        logger.error(f"错误类型: {type(e).__name__}")
        logger.error(f"错误详情: {str(e)}")
        logger.error(f"邮件发送失败: 收件人={email}, 错误详情={str(e)}")
        return False

def verify_code(email, code):
    """验证用户输入的验证码"""
    logger.info(f"开始验证邮箱 {email} 的验证码")
    
    if email not in verification_codes:
        logger.warning(f"邮箱 {email} 没有对应的验证码记录")
        return False
    
    stored_code = verification_codes[email]
    if datetime.now() > stored_code['expiration']:
        logger.warning(f"邮箱 {email} 的验证码已过期")
        del verification_codes[email]
        return False
    
    if stored_code['code'] != code:
        logger.warning(f"邮箱 {email} 提供的验证码不正确")
        return False
    
    logger.info(f"邮箱 {email} 的验证码验证成功")
    del verification_codes[email]
    return True

def reset_ad_password(username, new_password):
    """重置Active Directory用户密码"""
    logger.info(f"开始重置用户 {username} 的密码")
    try:
        server = ldap3.Server(LDAP_SERVER, port=LDAP_PORT, use_ssl=False)
        # 使用与get_user_email_from_ad相同的认证格式
        domain = LDAP_BASE_DN.split(',')[0].replace('DC=', '')
        user_dn = f"{domain}\\{LDAP_USER_DN.split(',')[0].replace('CN=', '')}"
        
        conn = ldap3.Connection(
            server,
            user=user_dn,
            password=LDAP_PASSWORD,
            authentication=ldap3.SIMPLE,
            auto_bind=False,
            auto_referrals=False
        )
        
        if not conn.bind():
            logger.error(f"LDAP连接失败: {conn.result}")
            return False, "LDAP连接失败"
        
        user_dn = f'CN={username},{LDAP_BASE_DN}'
        conn.extend.microsoft.modify_password(user_dn, new_password)
        logger.info(f"用户 {username} 的密码重置成功")
        return True, "密码重置成功"
    
    except Exception as e:
        logger.error(f"重置用户 {username} 密码失败: {str(e)}")
        return False, f"密码重置失败: {str(e)}"

@app.route('/api/send-code', methods=['POST'])
def send_code():
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')
    
    if not email:
        logger.warning("收到发送验证码请求，但邮箱地址为空")
        return jsonify({'success': False, 'message': '邮箱地址不能为空'}), 400
    
    if not username:
        logger.warning("收到发送验证码请求，但用户名为空")
        return jsonify({'success': False, 'message': '用户名不能为空'}), 400
    
    logger.info(f"收到发送验证码请求: username={username}, email={email}")
    
    # 验证邮箱地址是否匹配
    ad_email = get_user_email_from_ad(username)
    if not ad_email:
        return jsonify({'success': False, 'message': '未找到用户或无法获取用户信息'}), 500
    
    if not ad_email or email.lower() != ad_email.lower():
        logger.warning(f"用户提供的邮箱地址与域中的不匹配: provided={email}, actual={ad_email}")
        return jsonify({'success': False, 'message': '未找到用户或无法获取用户信息'}), 500
    
    try:
        if send_verification_code(email):
            return jsonify({'success': True, 'message': '验证码已发送'}), 200
        else:
            logger.error(f"验证码发送失败: email={email}")
            return jsonify({'success': False, 'message': '验证码发送失败'}), 500
    except Exception as e:
        logger.error(f"验证码发送异常: {str(e)}")
        return jsonify({'success': False, 'message': '验证码发送失败，请稍后重试'}), 500

@app.route('/api/get-config', methods=['GET'])
def get_config():
    return jsonify({
        'api_base_url': f'http://{os.getenv("SERVER_IP", "localhost")}:5001/api'
    })

@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')
    code = data.get('code')
    new_password = data.get('new_password')
    
    logger.info(f"收到密码重置请求: 用户名={username}, 邮箱={email}")
    
    if not all([username, email, code, new_password]):
        logger.warning(f"密码重置请求缺少必要字段: username={username}, email={email}")
        return jsonify({'success': False, 'message': '所有字段都是必填的'}), 400
    
    # 再次验证邮箱地址是否匹配
    ad_email = get_user_email_from_ad(username)
    if not ad_email or email.lower() != ad_email.lower():
        logger.warning(f"密码重置时邮箱地址不匹配: provided={email}, actual={ad_email}")
        return jsonify({'success': False, 'message': '用户名或邮箱地址无效'}), 400

    if not verify_code(email, code):
        logger.warning(f"验证码验证失败: email={email}")
        return jsonify({'success': False, 'message': '验证码无效或已过期'}), 400
    
    success, message = reset_ad_password(username, new_password)
    if success:
        logger.info(f"密码重置成功: username={username}")
    else:
        logger.error(f"密码重置失败: username={username}, message={message}")
    return jsonify({'success': success, 'message': message}), 200 if success else 500

def main():
    """启动应用程序"""
    logger.info("密码重置服务启动")
    app.run(host='0.0.0.0', port=5001)

if __name__ == '__main__':
    main()