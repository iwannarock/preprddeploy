import logging
import subprocess
import time
import traceback
import random

import MySQLdb
from cassandra.cluster import Cluster
from cassandra.cluster import NoHostAvailable
import redis

from common.libs import ec2api
from common.libs.ansible_api import AnsibleRunner
from preprddeploy.settings import HOME_PATH

PRIVATE_KEY_PATH = '%s/pem' % HOME_PATH
logger = logging.getLogger('common')


class BasicService(object):
    def __init__(self, region):
        self.instances = self.get_basic_service_instances(region)
            
    def get_basic_service_instances(self, region):
        """
        find ec2 instances by basic service name
        Args:
            region (string): region name
        """
        instances = ec2api.find_basic_instances(region, [self.service_name])
        logger.debug('%s instancs are:' % self.service_name)
        for instance in instances:
            logger.debug(instance.private_ip_address)
        return instances

    def stop_service(self):
        """just stop all basic service instances and wait it change to stopped"""
        try:
            for instance in self.instances:
                instance.stop()
            return {'ret': True}
        except:
            logger.error('stop service failed: %s' % self.service_name)
            error_msg = traceback.format_exc()
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}

    def prework_before_start_service(self):
        """before start service,start services' instances and wait it change to running"""
        for instance in self.instances:
            instance.start()
        for instance in self.instances:
            # wait instance can fping success
            instance_ip = instance.private_ip_address
            ec2api.fping_instance(instance_ip)
        time.sleep(60)
        logger.info('pre work done.')


class MysqlBaseService(BasicService):
    def __init__(self, region):
        BasicService.__init__(self, region)
    
    def start_service(self):
        """
        start mysql service:
            1.start mysql instances.
            2.check if service has started.
        """
        self.prework_before_start_service()
        if self.check_service():
            logger.info('mysql service started.')
            return {'ret': True}
        else:
            error_msg = 'mysql service connot connect, service auto start failed.'
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        
    def check_service(self):
        """check if mysql service available"""
        for instance in self.instances:
            ip = instance.private_ip_address
            try:
                MySQLdb.connect(host=ip, user="cloud", passwd="tplinkcloud", connect_timeout=3)
            except:
                logger.error('mysql:%s service not running!' % ip)
                return False
        return True


class MysqlService(MysqlBaseService):
    def __init__(self, region):
        self.service_name = 'mysql'
        MysqlBaseService.__init__(self, region)


class BranchMainDbService(MysqlBaseService):
    def __init__(self, region):
        self.service_name = 'branchMainDb'
        MysqlBaseService.__init__(self, region)


class MongodbService(BasicService):
    def __init__(self, region):
        self.service_name = 'mongodb'
        BasicService.__init__(self, region)
        self.check_sh_path = '%s/mongodb-3.0.2/bin/show.sh' % HOME_PATH
        
    def start_service(self):
        """
        start mongo service:
            1.start mongo instances.
            2.check if service has started.
        """
        self.prework_before_start_service()
        if self.check_service():
            logger.info('mongodb service started.')
            return {'ret': True}
        else:
            error_msg = 'mongodb service start failed.'
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        
    def check_service(self):
        """check if mongo service available"""
        ip_list = []
        for instance in self.instances:
            ip_list.append(instance.private_ip_address)
            keyname = instance.key_name
        keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, keyname)
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args=self.check_sh_path,
                                   ip=','.join(ip_list),
                                   keyfile=keypath)
        ansible_results = ansible_runner.results
        if ansible_results[2] != len(ip_list):
            logger.warn('not all mongo instance pass state check. not connect: %s, check failed: %s, details: %s' % (
                ansible_results[1],
                ansible_results[3],
                ansible_results[0]['failed']
            ))
            return False
        else:
            for host, stdout in ansible_results[0]['ok'].items():
                if len(stdout) == 0:
                    logger.warn('mongo service not running in %s' % host)
                    return False
        return True


class ElasticsearchService(BasicService):
    def __init__(self, region):
        self.service_name = 'elasticsearch'
        BasicService.__init__(self, region)
        self.ip_list = []
        for instance in self.instances:
            self.key_name = instance.key_name
            self.ip_list.append(instance.private_ip_address)
        elasticsearch_dir = '%s/cloud-third/elasticsearch/elasticsearch-2.2.1' % HOME_PATH
        self.start_cmd = 'cd %s/bin&&nohup ./elasticsearch&' % elasticsearch_dir
        
    def start_service(self):
        self.prework_before_start_service()
        shell_cmd = "/bin/bash -c 'source /etc/profile&&%s'" % self.start_cmd
        keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args=shell_cmd,
                                   ip=','.join(self.ip_list),
                                   keyfile=keypath)
        ansible_results = ansible_runner.results
        if ansible_results[2] != len(self.ip_list):
            error_msg = 'not all elasticsearch start success, not connect: %s, start failed: %s, details: %s' % (
                ansible_results[1],
                ansible_results[3],
                ansible_results[0]['failed']
            )
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        # todo: elasticsearch service start need time not certain, whether just start?
        time.sleep(10)
        if not self.check_service():
            error_msg = 'elasticsearch nodes status not correct.'
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        logger.info('elasticsearch service started.')
        return {'ret': True}
    
    def check_service(self):
        max_retry_times = 3
        count = 0
        while count < max_retry_times:
            curl_result = self.__curl_service_page()
            if curl_result:
                return True
            else:
                count += 1
                time.sleep(10)
        return False

    def __curl_service_page(self):
        random_ip = random.choice(self.ip_list)
        check_cmd = 'curl -m 10 http://%s:9200/_cat/nodes?v' % random_ip
        check_process = subprocess.Popen(check_cmd, shell=True,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
        stdout, stderr = check_process.communicate()
        if check_process.poll():
            error_msg = 'execute check command failed,stderr: %s' % stderr
            logger.error(error_msg)
            return False
        not_up_node = []
        for ip in self.ip_list:
            if ip not in stdout:
                not_up_node.append(ip)
        if not_up_node:
            logger.warn('not all elasticsearch node is up: %s' % not_up_node)
            return False
        return True


class RedisClusterBaseService(BasicService):
    def __init__(self, region):
        BasicService.__init__(self, region)
        self.ip_list = []
        for instance in self.instances:
            self.key_name = instance.key_name
            self.ip_list.append(instance.private_ip_address)
        redis_conf_path = '%s/cloud-third/redis-cluster/conf/redis.conf' % HOME_PATH
        self.start_cmd = 'redis-server %s&&ps -ef|grep redis|grep cluster|grep -v grep' % redis_conf_path

    def start_service(self):
        """
        start redis service:
            1.start redis-server in all redis instances.
            2.check service: see if there has process redis-server in each instance.
        """
        self.prework_before_start_service()
        key_path = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args=self.start_cmd,
                                   ip=','.join(self.ip_list),
                                   keyfile=key_path)
        ansible_results = ansible_runner.results
        redis_count = len(self.ip_list)
        if ansible_results[2] != redis_count:
            error_msg = 'not all service start success, not connect: %s, start command failed: %s, details: %s' % (
                ansible_results[1],
                ansible_results[3],
                ansible_results[0]['failed']
            )
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        for host, stdout in ansible_results[0]['ok'].items():
            if 'redis-server' not in stdout:
                error_msg = 'can not found redis process in %s, start failed' % host
                logger.error(error_msg)
                return {'ret': False, 'msg': error_msg}
        logger.info('redisClusterNoFailoverMaster service started.')
        return {'ret': True}

    def check_service(self):
        """check redis service by using command redis.ping"""
        for ip in self.ip_list:
            redis_db = redis.Redis(host=ip, socket_connect_timeout=2)
            try:
                redis_db.ping()
            except:
                logger.error("redis service not running!")
                return False
        return True


class RedisClusterNoFailoverMasterService(RedisClusterBaseService):
    def __init__(self, region):
        self.service_name = 'redisClusterNoFailoverMaster'
        RedisClusterBaseService.__init__(self, region)


class RedisClusterNoFailoverSlaveService(RedisClusterBaseService):
    def __init__(self, region):
        self.service_name = 'redisClusterNoFailoverSlave'
        RedisClusterBaseService.__init__(self, region)


class RedisClusterMasterService(RedisClusterBaseService):
    def __init__(self, region):
        self.service_name = 'redisClusterMaster'
        RedisClusterBaseService.__init__(self, region)


class RedisClusterSlaveService(RedisClusterBaseService):
    def __init__(self, region):
        self.service_name = 'redisClusterSlave'
        RedisClusterBaseService.__init__(self, region)


class RedisClusterWithFailoverMasterService(RedisClusterBaseService):
    def __init__(self, region):
        self.service_name = 'redisClusterWithFailoverMaster'
        RedisClusterBaseService.__init__(self, region)
        redis_conf_path = '%s/cloud-third/redis/cluster-3.0.3/node.conf' % HOME_PATH
        self.start_cmd = 'redis-server %s&&ps aux|grep redis|grep -v grep' % redis_conf_path


class RedisClusterWithFailoverSlaveService(RedisClusterBaseService):
    def __init__(self, region):
        self.service_name = 'redisClusterWithFailoverSlave'
        RedisClusterBaseService.__init__(self, region)
        redis_conf_path = '%s/cloud-third/redis/cluster-3.0.3/node.conf' % HOME_PATH
        self.start_cmd = 'redis-server %s&&ps aux|grep redis|grep -v grep' % redis_conf_path


class CassandraBaseService(BasicService):
    def __init__(self, region):
        BasicService.__init__(self, region)
        cass_bin_path = '%s/cloud-third/cassandra/apache-cassandra-2.1.5/bin' % HOME_PATH
        self.startCmd = '/bin/bash -c "source /etc/profile;nohup %s/cassandra -f > %s/cas_out&"' % (cass_bin_path,
                                                                                                    HOME_PATH)
        dest_dict = {}
        self.name_instance_dict = {}
        for instance in self.instances:
            self.key_name = instance.key_name
            instance_ip = instance.private_ip_address
            instance_name = ec2api.get_instance_tag_name(instance)
            dest_dict.update({instance_name: instance_ip})
            self.name_instance_dict.update({instance_name: instance})
        self.instances_list = dest_dict.items()
        self.instances_list.sort()

    def start_service(self):
        """
        start cassandra service:
            1.start the first instance and start service.
            2.after 1 min,check the service state.
            3.loop step 1 and 2 till all cassandra service start.
        """
        self.prework_before_start_service()
        for instance_name, instance_ip in self.instances_list:
            logger.info('start service: %s' % instance_name)
            keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
            ansible_runner = AnsibleRunner()
            ansible_runner.run_ansible(module_args=self.startCmd, ip=instance_ip, keyfile=keypath)
            start_results = ansible_runner.results
            if start_results[2] != 1:
                error_msg = 'start %s failed: %s' % (instance_name, start_results[0]['failed'][instance_ip])
                logger.error(error_msg)
                return {'ret': False, 'msg': error_msg}
            logger.info('wait %s service be available' % instance_name)
            waittime = 120
            starttime = time.time()
            check_cmd = 'grep "No gossip backlog; proceeding" ~/cas_out'
            while True:
                ansible_runner.run_ansible(module_args=check_cmd, ip=instance_ip, keyfile=keypath)
                check_results = ansible_runner.results
                if check_results[0]['ok'] and 'No gossip backlog; proceeding' in check_results[0]['ok'][instance_ip]:
                    logger.info('cassandra service started: %s' % instance_name)
                    break
                else:
                    if time.time()-starttime > waittime:
                        error_msg = '%s service not running in %s seconds' % (instance_name, waittime)
                        logger.error(error_msg)
                        return {'ret': False, 'msg': error_msg}
                time.sleep(10)
        logger.info('all %s service started' % self.service_name)
        return {'ret': True}

    def stop_service(self):
        self.instances_list.sort(reverse=True)
        for instance_name, instance_ip in self.instances_list:
            logging.info('kill cassandra process in %s' % instance_name)
            kill_cmd = '''ansible all -i %s, -m shell -a "ps -ef|grep cass|grep -v grep|awk '{print \$2}'|xargs kill"
            --private-key %s/%s.pem''' % (
                instance_ip,
                PRIVATE_KEY_PATH,
                self.key_name
            )
            p = subprocess.Popen(kill_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.communicate()
            if p.poll() == 0:
                logging.info('kill cassandra process success')
                time.sleep(10)
            instance = self.name_instance_dict[instance_name]
            instance.stop()
        return {'ret': True}

    def check_service(self):
        """check if the cassandra cluster can connect"""
        for instance_name, instance_ip in self.instances_list:
            cluster = Cluster(contact_points=[instance_ip], port=9042,
                              cql_version="3.2.0", protocol_version=3,
                              reconnection_policy=None)
            try:
                session = cluster.connect(keyspace="cloud")
                session.shutdown()
            except NoHostAvailable:
                logger.info("cassandra service not running in %s" % instance_name)
                return False
            finally:
                cluster.shutdown()
        return True


class CassandraService(CassandraBaseService):
    def __init__(self, region):
        self.service_name = 'cassandra'
        CassandraBaseService.__init__(self, region)


class PushCassandraService(CassandraBaseService):
    def __init__(self, region):
        self.service_name = 'pushCassandra'
        CassandraBaseService.__init__(self, region)


class FactoryInfoCassandraService(CassandraBaseService):
    def __init__(self, region):
        self.service_name = 'factoryInfoCassandra'
        CassandraBaseService.__init__(self, region)


class StatsCassandraService(CassandraBaseService):
    def __init__(self, region):
        self.service_name = 'statsCassandra'
        CassandraBaseService.__init__(self, region)
        cass_bin_path = '%s/cloud-third/cassandra/apache-cassandra-2.1.17/bin' % HOME_PATH
        self.startCmd = '/bin/bash -c "source /etc/profile;nohup %s/cassandra -f > %s/cas_out&"' % (cass_bin_path,
                                                                                                    HOME_PATH)


class ZookeeperService(BasicService):
    def __init__(self, region):
        self.service_name = 'zookeeper'
        BasicService.__init__(self, region)
        self.ip_list = []
        for instance in self.instances:
            self.key_name = instance.key_name
            self.ip_list.append(instance.private_ip_address)
        self.zookeeper_dir = '%s/cloud-third/zookeeper/zookeeper-3.4.6' % HOME_PATH
        
    def start_service(self):
        """
        start zookeeper service:
            1.delete all the files in the fold ${zookeeper_dir}/data,except myid.
            2.delete zookeeper log files.
            3.start service in all zookeeper instances.use command :./zkServer.sh start.
            4.check output to make sure the service started.
        """
        self.prework_before_start_service()
        delete_commands = [
           "cd %s/data;rm -rf `ls|grep -v myid`" % self.zookeeper_dir,
           "rm -rf %s/log/*" % self.zookeeper_dir
        ]
        cmd = '&&'.join(delete_commands)
        keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args=cmd, ip=','.join(self.ip_list), keyfile=keypath)
        rm_results = ansible_runner.results
        zk_count = len(self.ip_list)
        if rm_results[2] != zk_count:
            error_msg = 'rm zookeeper data failed. not connect: %s, run rm cmd failed: %s, details: %s' % (
                rm_results[1],
                rm_results[3],
                rm_results[0]['failed']
            )
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        logger.info('delete file success')
        start_zk_cmd = '/bin/bash -c "source /etc/profile;%s/bin/zkServer.sh start"' % self.zookeeper_dir
        ansible_runner.run_ansible(module_args=start_zk_cmd, ip=','.join(self.ip_list), keyfile=keypath)
        start_results = ansible_runner.results
        if start_results[2] != zk_count:
            error_msg = 'run start command failed, not connect: %s, failed: %s, detail msg: %s' % (
                start_results[1],
                start_results[3],
                start_results[0]['failed']
            )
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        time.sleep(10)  # wait zookeeper vote leader
        if self.check_service():
            logger.info('zookeeper service started')
            return {'ret': True}
        return {'ret': False, 'msg': 'zookeeper service state not correct'}
    
    def check_service(self):
        """check zookeeper service state. use command zkServer.sh status"""
        check_cmd = '/bin/bash -c "source /etc/profile;%s/bin/zkServer.sh status"' % self.zookeeper_dir
        keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args=check_cmd, ip=','.join(self.ip_list), keyfile=keypath)
        check_results = ansible_runner.results
        zk_instance_count = len(self.ip_list)
        if zk_instance_count == 1:
            if 'standalone' in check_results[0]['ok'][self.ip_list[0]]:
                return True
            return False
        if check_results[2] != zk_instance_count:
            logger.error('run zookeeper check command failed. not connect: %s, failed: %s, details: %s' % (
                check_results[1],
                check_results[3],
                check_results[0]['failed']
            ))
            return False
        mode_dict = {'leader': 0, 'follower': 0}
        for host, stdout in check_results[0]['ok'].items():
            if 'follower' in stdout:
                mode_dict['follower'] += 1
            elif 'leader' in stdout:
                mode_dict['leader'] += 1
            else:
                logger.warn('%s is not a zookeeper leader or follower' % host)
        leader_num = mode_dict['leader']
        follower_num = mode_dict['follower']
        if leader_num == 1 and leader_num + follower_num == zk_instance_count:
            return True
        logger.warn('not all zk status correct. zk instance number: %s, leader num: %s, follower num: %s' % (
            zk_instance_count,
            leader_num,
            follower_num
        ))
        return False


class KafkaBaseService(BasicService):
    def __init__(self, region):
        BasicService.__init__(self, region)
        self.kafka_dir = '%s/cloud-third/kafka/kafka_2.11-0.9.0.0' % HOME_PATH
        self.start_cmd = 'cd %s/bin;nohup ./kafka-server-start.sh ../config/server.properties&' % self.kafka_dir
        self.ip_list = []
        for instance in self.instances:
            self.key_name = instance.key_name
            self.ip_list.append(instance.private_ip_address)
            
    def start_service(self):
        """
            start kafka and check process
        """
        self.prework_before_start_service()
        start_cmd = '/bin/bash -c "source /etc/profile;%s"' % self.start_cmd
        keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args=start_cmd, ip=','.join(self.ip_list), keyfile=keypath)
        start_results = ansible_runner.results
        if start_results[2] != len(self.ip_list):
            error_msg = 'start all kafka service failed, not connect: %s, start failed: %s, detail msg: %s' % (
                start_results[1],
                start_results[3],
                start_results[0]['failed']
            )
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        if not self.check_service():
            error_msg = "kafka service process is not found, it doesn't running"
            logger.error(error_msg)
            return {'ret': False, 'msg': error_msg}
        return {'ret': True}
    
    def check_service(self):
        ansible_runner = AnsibleRunner()
        ansible_runner.run_ansible(module_args='ps -ef|grep kafka|grep -v grep',
                                   ip=','.join(self.ip_list),
                                   keyfile='%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name))
        check_ps_ret = ansible_runner.results_raw
        dark = check_ps_ret.get('dark')
        if dark:
            logger.warn('some instance can not connect: %s' % dark.keys())
            return False
        contacted = check_ps_ret.get('contacted')
        for ip, check_info in contacted.items():
            if check_info['rc'] or not check_info['stdout']:
                logger.warn('can not find kafka process in %s' % ip)
                return False
        return True


class KafkaService(KafkaBaseService):
    def __init__(self, region):
        self.service_name = 'kafka'
        KafkaBaseService.__init__(self, region)


class LogkafkaService(KafkaBaseService):
    def __init__(self, region):
        self.service_name = 'logkafka'
        KafkaBaseService.__init__(self, region)


class RabbitmqService(BasicService):
    def __init__(self, region):
        self.service_name = 'rabbitmq'
        BasicService.__init__(self, region)
        self.ip_list = []
        dest_dict = {}
        for instance in self.instances:
            self.key_name = instance.key_name
            self.ip_list.append(instance.private_ip_address)
            instance_name = ec2api.get_instance_tag_name(instance)
            dest_dict.update({instance_name: instance})
        self.instance_list = dest_dict.items()
        # sort all rabbitmq instance by Name tag. make sure the right order when start service.
        self.instance_list.sort()

    def start_service(self):
        """
        start rabbitmq service:
            rabbitmq service can autostart when instance start,
            but must start instance with a right order.
            1.first start instance:rabbitmq-a in en or rabbitmq-a-0 in cn.and make sure the instance has already running.
            2.start the other instance.and make sure it change to running.
            3.check the rabbitmq cluster state by using command:sudo rabbitmqctl cluster_status
        """
        for instance_name, instance in self.instance_list:
            logger.info('start instance: %s' % instance_name)
            instance.start()
            ec2api.wait_instance_running(instance)
            logger.info('rabbitmq instance started: %s' % instance_name)
        time.sleep(20)
        max_retry = 3
        while max_retry > 0:
            if not self.check_service():
                max_retry -= 1
                if max_retry == 0:
                    error_msg = 'rabbitmq service not autostart after test %s times' % max_retry
                    logger.error(error_msg)
                    return {'ret': False, 'msg': error_msg}
            else:
                return {'ret': True}
 
    def check_service(self):
        check_cmd = "rabbitmqctl cluster_status"
        ansible_runner = AnsibleRunner()
        keypath = '%s/%s.pem' % (PRIVATE_KEY_PATH, self.key_name)
        ansible_runner.run_ansible(module_args=check_cmd, ip=','.join(self.ip_list),
                                   keyfile=keypath, become=True)
        check_ret = ansible_runner.results_raw
        dark = check_ret.get('dark')
        if dark:
            logger.warn('some rabbitmq instance can not connect: %s' % dark.keys())
            return False
        contacted = check_ret.get('contacted')
        for ip, check_info in contacted.items():
            if check_info['rc'] or 'Error' in check_info['stdout']:
                logger.warn('rabbitmq service not running in %s' % ip)
                return False
        return True
    
    def stop_service(self):
        """stop the rabbitmq instance in a reverse order of the start order"""
        dest_list = self.instance_list[:]
        dest_list.reverse()
        for instance_name, instance in dest_list:
            try:
                instance.stop()
                ec2api.wait_instance_stopped(instance)
            except:
                logger.error('stop service failed: %s' % self.service_name)
                error_msg = traceback.format_exc()
                logger.error(error_msg)
                return {'ret': False, 'msg': error_msg}
        return {'ret': True}

class RedisService(BasicService):
    def __init__(self, region):
        self.service_name = 'redis'
        BasicService.__init__(self, region)
        self.privateIpList = []
        for instance in self.instances:
            self.keyPairName = instance.key_name
            self.privateIpList.append(instance.private_ip_address)
        redisConfPath = '%s/redis/config-redis/6379/redis.conf'%HOME_PATH
        self.startCmd = 'redis-server %s&&ps aux|grep redis|grep -v grep'%redisConfPath
        
    def start_service(self):
        """
        start redis service:
            1.start redis-server in all redis instances.
            2.check service: see if there has process redis-server in each instance.
        """
        self.prework_before_start_service()
        command = "ansible all -i %s, -m shell -a '%s' --private-key %s/%s.pem"%(
                                                                                    ','.join(self.privateIpList),
                                                                                    self.startCmd,
                                                                                    PRIVATE_KEY_PATH,
                                                                                    self.keyPairName           
                                                                                            )
        p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout,stderr = p.communicate()
        redisInstanceCount = len(self.privateIpList)
        if stdout.count('redis-server') == redisInstanceCount:
            logger.info('redis service started')
            return {'ret': True}
        else:
            logger.error('redis service start failed')
            logger.error(stdout)
            return {'ret': False, 'msg': stdout}
            
    def check_service(self):
        """check redis service by using command redis.ping"""
        for ip in self.privateIpList:
            redisDb = redis.Redis(host=ip, socket_connect_timeout=2)
            try:
                redisDb.ping()
            except:
                logger.error("redis service not running!")
                return False
        return True

class CodisService(BasicService):
    def __init__(self, region):
        self.service_name = 'codis'
        BasicService.__init__(self, region)
        self.codisDir = '%s/cloud-codis'%HOME_PATH
        self.privateIpList = []
        destInfoDict = {}
        for instance in self.instances:
            self.keyPairName = instance.key_name
            instanceIp = instance.private_ip_address
            self.privateIpList.append(instanceIp)
            instanceTags = instance.tags
            for tag in instanceTags:
                if tag['Key'] == 'Name':
                    instanceName = tag['Value']
                    break
            destInfoDict.update({instanceName:instanceIp})
        # choose the first instance(order by name) to start dashboard.
        instancesList = destInfoDict.items()
        instancesList.sort() 
        self.dashboardName = instancesList[0][0]
        self.dashboardIp = instancesList[0][1]
        
    def start_service(self):
        """
        start codis service:
            1.check zookeeper service state.codis must start after zookeeper service running.
            2.start redis server in all codis instance.
            3.in dashboard instance,start dashboard.init slot,add server master and set slot range.
            4.start codis proxy.
        """       
        self.prework_before_start_service()
        startRedisResult = self._startRedisInCodisInstance()
        if startRedisResult != True:
            return {'ret': False, 'msg': startRedisResult}
        startDashboardResult = self._startDashboard()
        if startDashboardResult != True:
            return {'ret': False, 'msg': startDashboardResult}
        slotInitResult = self._slotInit()
        if slotInitResult != True:
            return {'ret': False, 'msg': slotInitResult}
        setSlotResult =  self._setSlot()
        if setSlotResult != True:
            return {'ret': False, 'msg': setSlotResult}
        startProxyResult = self._startCodisProxy()
        if startProxyResult != True:
            return {'ret': False, 'msg': startProxyResult}
        return {'ret': True}
    
    def check_service(self):
        """check if codis cluster can connect."""
        for ip in self.privateIpList:
            redisDb = redis.Redis(host=ip, socket_connect_timeout=2)
            try:
                redisDb.ping()
            except:
                return False
        return True
        
    def _startRedisInCodisInstance(self):
        """start redis-server in all codis instances."""
        startRedisCmd = 'cd %s; ./redis redis.conf&&ps aux|grep redis|grep -v grep'%self.codisDir
        command = "ansible all -i %s, -m shell -a '%s' --private-key %s/%s.pem"%(
                                                                            ','.join(self.privateIpList),
                                                                            startRedisCmd,
                                                                            PRIVATE_KEY_PATH,
                                                                            self.keyPairName           
                                                                            )
        p = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout,stderr = p.communicate()
        redisInstanceCount = len(self.privateIpList)
        if stdout.count('redis *:6379') == redisInstanceCount:
            logger.info('redis service started')
            return True
        else:
            logger.error('redis service start failed')
            logger.error(stdout)
            return stdout
    
    def _startDashboard(self):
        """start dashboard in dashboard instance"""
        startCmd = 'ansible all -i %s, -m shell -a "cd %s;nohup ./codis-config dashboard&" --private-key %s/%s.pem'%(
                                                                                                                self.dashboardIp,
                                                                                                                self.codisDir,
                                                                                                                PRIVATE_KEY_PATH,
                                                                                                                self.keyPairName
                                                                                                                )                                                                                                                      
        p = subprocess.Popen(startCmd, shell=True)
        p.communicate()
        time.sleep(1)
        checkCmd = "ansible all -i %s, -m shell -a 'ps aux|grep dashboard' --private-key %s/%s.pem"%(
                                                                                                self.dashboardIp,
                                                                                                PRIVATE_KEY_PATH,
                                                                                                self.keyPairName
                                                                                                )
        p = subprocess.Popen(checkCmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout,stderr = p.communicate()
        if 'codis-config dashboard' not in stdout:
            logger.error('start dashboard in %s failed.'%self.dashboardName)
            logger.error(stdout)
            return stdout
        logger.info('start dashboard in %s success.'%self.dashboardName)
        return True
    
    def _slotInit(self):
        """init slot in dashboard instance"""
        startCmd = "ansible all -i %s, -m shell -a 'cd %s;./codis-config slot init' --private-key %s/%s.pem"%(
                                                                                                        self.dashboardIp,
                                                                                                        self.codisDir,
                                                                                                        PRIVATE_KEY_PATH,
                                                                                                        self.keyPairName
                                                                                                        )
        p = subprocess.Popen(startCmd, shell=True, stdout=subprocess.PIPE)
        stdout,stderr = p.communicate()
        if p.poll() or '"msg": "OK"' not in stdout:
            logger.error('slot init failed.')
            logger.error(stdout)
            return stdout
        logger.info('slot init success.')
        return True
        
    def _setSlot(self):
        """add server master and set slot range"""
        serverId = 0
        for ip in self.privateIpList:
            serverId += 1
            shellArgString = "cd %s;./codis-config server add %s %s:6379 master"%(
                                                                            self.codisDir,
                                                                            serverId,
                                                                            ip
                                                                            )
            cmd = 'ansible all -i %s, -m shell -a "%s" --private-key %s/%s.pem'%(
                                                                            self.dashboardIp,
                                                                            shellArgString,
                                                                            PRIVATE_KEY_PATH,
                                                                            self.keyPairName
                                                                            )
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
            stdout,stderr = p.communicate()
            if p.poll() or '"msg": "OK"' not in stdout:
                logger.error('add master server failed. %s'%ip)
                logger.error(stdout)
                exit(1)
            logger.debug('add master server success: %s, server id:%s'%(ip,serverId))
        logger.info('add all master server success.')
        slotRangeSetCmds = [
            'cd %s;./codis-config slot range-set 0 340 1 online'%self.codisDir,
            'cd %s;./codis-config slot range-set 341 680 2 online'%self.codisDir,
            'cd %s;./codis-config slot range-set 681 1023 3 online'%self.codisDir
        ]   
        instanceNum = len(self.privateIpList)
        # cn has two codis instances and en has three instances.
        # set slot range according to the codis instance count.
        slotRangeSetCmds = slotRangeSetCmds[:instanceNum]
        for rangeSetCmd in slotRangeSetCmds:
            ansibleCommand = 'ansible all -i %s, -m shell -a "%s" --private-key %s/%s.pem'%(
                                                                                        self.dashboardIp,
                                                                                        rangeSetCmd,
                                                                                        PRIVATE_KEY_PATH,
                                                                                        self.keyPairName
                                                                                        )
            p = subprocess.Popen(ansibleCommand, shell=True, stdout=subprocess.PIPE)
            stdout,stderr = p.communicate()
            if p.poll() or '"msg": "OK"' not in stdout:
                logger.error('slot range-set failed. %s'%ip)
                logger.error(stdout)
                return stdout
        logger.info('slot range set success')
        return True
    
    def _startCodisProxy(self):
        """start codis proxy"""
        for ip in self.privateIpList:
            startCodisCommand = "./codis-proxy -c config.ini -L ./log/proxy.log" + \
                                " --cpu=8 --addr=%s:19000 --http-addr=%s:11000"%(ip,ip)
            ansibleCommand = "ansible all -i %s, -m shell -a 'cd %s;nohup %s &' --private-key %s/%s.pem"%(
                                                                                                    ip,
                                                                                                    self.codisDir,
                                                                                                    startCodisCommand,
                                                                                                    PRIVATE_KEY_PATH,
                                                                                                    self.keyPairName
                                                                                                    )
            p = subprocess.Popen(ansibleCommand, shell=True)
            p.communicate()
            time.sleep(1)
            checkShellString = "ps aux|grep codis-proxy|grep -v grep"
            checkCommand = 'ansible all -i %s, -m shell -a "%s" --private-key %s/%s.pem'%(
                                                                                    ip,
                                                                                    checkShellString,
                                                                                    PRIVATE_KEY_PATH,
                                                                                    self.keyPairName
                                                                                    )
            p = subprocess.Popen(checkCommand, shell=True, stdout=subprocess.PIPE)
            stdout,stderr = p.communicate()
            if p.poll() or startCodisCommand not in stdout:
                logger.error('start codis proxy failed. %s'%ip)
                logger.error(stdout)
                return stdout
        logger.info('all codis proxy start success.')
        proxyId = self._getDashBoardProxyId()
        shellStringArg = 'cd %s;./codis-config -c config.ini proxy online %s'%(
                                                                            self.codisDir,
                                                                            proxyId
                                                                            )
        proxyOnlineCommand ="ansible all -i %s, -m shell -a '%s' --private-key %s/%s.pem"%(
                                                                                    self.dashboardIp,
                                                                                    shellStringArg,
                                                                                    PRIVATE_KEY_PATH,
                                                                                    self.keyPairName
                                                                                    )
        p = subprocess.Popen(proxyOnlineCommand, shell=True, stdout=subprocess.PIPE)
        stdout,stderr = p.communicate()
        if p.poll() or '"msg": "OK"' not in stdout:
            logger.error('proxy online failed. %s'%self.dashboardIp)
            logger.error(stdout)
            return stdout
        logger.info('codis service started.')
        return True
        
    def _getDashBoardProxyId(self):
        """get the proxy_id arg value in the file config.ini"""
        getDashBoardProxyIdCmd = "ansible all -i %s, -m shell -a 'grep proxy_id %s/config.ini' --private-key %s/%s.pem"%(
                                                                                                            self.dashboardIp,
                                                                                                            self.codisDir,
                                                                                                            PRIVATE_KEY_PATH,
                                                                                                            self.keyPairName
                                                                                                            )
        p = subprocess.Popen(getDashBoardProxyIdCmd, shell=True, stdout=subprocess.PIPE)
        stdout,stderr = p.communicate()
        try:
            proxyId = stdout.split('proxy_id')[1]
        except IndexError:
            logger.error('not found param proxy_id in config.ini.')
            exit(1)
        proxyId = proxyId.strip()[1:]
        return proxyId
