monitoring_home=~/aws-parallelcluster-monitoring

start:
        docker-compose --env-file /etc/parallelcluster/cfnconfig -f ${monitoring_home}/docker-compose/docker-compose.headnode.yml -p monitoring-headnode up -d

stop:
        docker-compose --env-file /etc/parallelcluster/cfnconfig -f ${monitoring_home}/docker-compose/docker-compose.headnode.yml -p monitoring-headnode down